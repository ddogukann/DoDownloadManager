import os
import re
import shutil
from urllib.parse import urlsplit
import threading
import time
import uuid
from pathlib import Path

from .config import TEMP_DIR
from .engine import RateLimiter, SegmentedDownloader
from .store import Store
from .util import CATEGORY_FOLDERS, category_for, filename_from_url, sanitize_filename, unique_path
from .video import VideoDownloader
from .netlanes import iface_exists
from .post import PostProcessor
from .virustotal import BUSY_STATES, VTService

ACTIVE_STATES = ("running", "queued")


class DownloadManager:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.store = store or Store()
        self.lock = threading.RLock()
        self.items = {}
        self.jobs = {}
        self.listeners = []
        self.limiter = RateLimiter(int(settings.get("speed_limit_kib") or 0) * 1024)
        self._last_persist = {}
        self.shutting_down = False
        self.holds = set()
        self.metered_limit = 0
        settings.connect(self._on_setting)
        self.vt = VTService(settings, self._vt_patch)
        self.post = PostProcessor(self)
        auto = settings.get("auto_resume")
        pending = []
        for rec in self.store.all():
            if rec["status"] in ACTIVE_STATES:
                rec["status"] = "queued" if auto else "paused"
                self.store.update(rec["id"], status=rec["status"])
            self.items[rec["id"]] = rec
            vt = rec.get("vt") or {}
            if rec["status"] == "done" and rec.get("path") and vt.get("state") in BUSY_STATES:
                pending.append(rec)
        for rec in pending:
            if os.path.exists(rec["path"]):
                self.vt.check_file(rec["id"], rec["path"], (rec.get("vt") or {}).get("expected"))

    def _on_setting(self, key, value):
        if key == "speed_limit_kib":
            self._apply_rate()
        elif key in ("max_active", "sched_start", "sched_end"):
            self.enforce_schedule()

    def _apply_rate(self):
        user = int(self.settings.get("speed_limit_kib") or 0) * 1024
        extra = int(self.metered_limit or 0) * 1024
        rates = [r for r in (user, extra) if r > 0]
        self.limiter.set_rate(min(rates) if rates else 0)

    def set_metered_limit(self, kib):
        self.metered_limit = int(kib or 0)
        self._apply_rate()

    @staticmethod
    def _parse_hm(value, default):
        try:
            h, m = str(value).strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
        except (ValueError, AttributeError):
            pass
        return default

    def in_window(self, now=None):
        lt = time.localtime(now) if now else time.localtime()
        cur = lt.tm_hour * 60 + lt.tm_min
        start = self._parse_hm(self.settings.get("sched_start"), 120)
        end = self._parse_hm(self.settings.get("sched_end"), 420)
        if start == end:
            return True
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end

    def set_hold(self, reason, on):
        changed = False
        with self.lock:
            if on and reason not in self.holds:
                self.holds.add(reason)
                changed = True
                jobs = list(self.jobs.items())
                for did, _ in jobs:
                    self.items[did]["_wanted"] = "requeue"
            elif not on and reason in self.holds:
                self.holds.discard(reason)
                changed = True
                jobs = []
            else:
                jobs = []
        for _, job in jobs:
            job.stop()
        if changed and not on:
            self.schedule()
        return changed

    def set_scheduled(self, did, scheduled):
        with self.lock:
            rec = self.items.get(did)
            if not rec or rec["status"] == "done":
                return
            opts = dict(rec.get("options") or {})
            if scheduled:
                opts["scheduled"] = True
            else:
                opts.pop("scheduled", None)
            rec["options"] = opts
            snap = dict(rec)
        self.store.update(did, options=opts)
        self._emit("updated", snap)
        if scheduled and rec["status"] in ("paused", "error"):
            self.resume(did)
        self.enforce_schedule()

    def enforce_schedule(self):
        if self.in_window():
            self.schedule()
            return
        with self.lock:
            jobs = [(did, job) for did, job in self.jobs.items()
                    if (self.items[did].get("options") or {}).get("scheduled")]
            for did, _ in jobs:
                self.items[did]["_wanted"] = "requeue"
        for _, job in jobs:
            job.stop()
        self.schedule()

    def connect(self, cb):
        self.listeners.append(cb)

    def _emit(self, kind, rec):
        snapshot = dict(rec)
        for cb in list(self.listeners):
            try:
                cb(kind, snapshot)
            except Exception:
                pass

    def records(self):
        with self.lock:
            return [dict(r) for r in sorted(self.items.values(), key=lambda r: (r.get("created") or 0))]

    def get(self, did):
        with self.lock:
            rec = self.items.get(did)
            return dict(rec) if rec else None

    def find_active_by_url(self, url):
        with self.lock:
            for r in self.items.values():
                if r["url"] == url and r["status"] != "done":
                    return dict(r)
        return None

    def rule_for(self, name, url):
        name = (name or "").casefold()
        host = (urlsplit(url or "").hostname or "").lower()
        for rule in self.settings.get("rules") or []:
            if not rule.get("enabled", True):
                continue
            exts = [e.strip().lower().lstrip(".") for e in (rule.get("exts") or "").split(",") if e.strip()]
            rhost = (rule.get("host") or "").strip().lower()
            contains = (rule.get("contains") or "").strip().casefold()
            if not (exts or rhost or contains):
                continue
            if exts and not any(name.endswith("." + e) for e in exts):
                continue
            if rhost and not (host == rhost or host.endswith("." + rhost)):
                continue
            if contains and contains not in name:
                continue
            return rule
        return None

    def rule_directory(self, name, url):
        rule = self.rule_for(name, url)
        if rule and (rule.get("folder") or "").strip():
            return os.path.expanduser(rule["folder"].strip())
        return None

    def set_post(self, did, patch):
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return
            post = dict(rec.get("post") or {})
            post.update(patch)
            rec["post"] = post
            snap = dict(rec)
        self.store.update(did, post=post)
        self._emit("updated", snap)

    def emit_alert(self, did, kind):
        snap = self.get(did)
        if snap:
            snap["_alert"] = kind
            self._emit("vt_alert", snap)

    def emit_simple(self, kind, did, path):
        snap = self.get(did)
        if snap:
            snap["_path"] = path
            self._emit(kind, snap)

    def default_directory(self, category):
        base = Path(os.path.expanduser(self.settings.get("download_dir")))
        if self.settings.get("category_subfolders") and category in CATEGORY_FOLDERS:
            return base / CATEGORY_FOLDERS[category]
        return base

    def add(self, url, kind="file", filename=None, directory=None, headers=None, options=None,
            start=True, page_url=None, connections=None):
        options = dict(options or {})
        if filename:
            options["filename"] = sanitize_filename(filename)
        if connections:
            options["connections"] = int(connections)
        if directory:
            options["custom_dir"] = True
        if kind == "video" and "own_folder" not in options:
            options["own_folder"] = bool(self.settings.get("video_own_folder"))
        if kind == "video":
            category = "audio" if options.get("audio_only") else "video"
        else:
            category = category_for(filename or url.split("?")[0])
        if not directory:
            directory = self.rule_directory(filename or filename_from_url(url), url)
        directory = str(directory or self.default_directory(category))
        with self.lock:
            pos = max([r.get("queue_pos") or 0 for r in self.items.values()] + [0]) + 1
            rec = {
                "id": uuid.uuid4().hex[:16], "url": url, "kind": kind,
                "filename": options.get("filename") or options.get("title") or "",
                "directory": directory, "path": None, "total": None, "downloaded": 0,
                "status": "queued" if start else "paused", "error": None, "category": category,
                "created": time.time(), "finished": None, "headers": headers or {}, "options": options,
                "segments": None, "etag": None, "last_modified": None, "resumable": False,
                "queue_pos": pos, "page_url": page_url,
            }
            self.items[rec["id"]] = rec
        self.store.insert(rec)
        self._emit("added", rec)
        if start:
            self.schedule()
        return dict(rec)

    def _update(self, did, emit=True, **fields):
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return None
            rec.update(fields)
            snap = dict(rec)
        self.store.update(did, **fields)
        if emit:
            self._emit("updated", snap)
        return snap

    def schedule(self):
        if self.shutting_down or self.holds:
            return
        window = self.in_window()
        with self.lock:
            limit = max(1, int(self.settings.get("max_active") or 3))
            running = sum(1 for r in self.items.values() if r["status"] == "running")
            queued = sorted((r for r in self.items.values() if r["status"] == "queued"
                             and (window or not (r.get("options") or {}).get("scheduled"))),
                            key=lambda r: (r.get("queue_pos") or 0, r.get("created") or 0))
            to_start = queued[: max(0, limit - running)]
            for rec in to_start:
                rec["status"] = "running"
                rec["error"] = None
        for rec in to_start:
            self.store.update(rec["id"], status="running", error=None)
            self._emit("updated", rec)
            vt = rec.get("vt") or {}
            if (rec["kind"] == "file" and self.settings.get("vt_url_check") and self.vt.enabled()
                    and not vt.get("url_state")):
                self.vt.check_url(rec["id"], rec["url"])
            threading.Thread(target=self._run, args=(rec["id"],), daemon=True, name=f"dl-{rec['id']}").start()

    def _resolve_path(self, did, info):
        with self.lock:
            rec = self.items[did]
            name = (rec.get("options") or {}).get("filename") or info.get("filename") or "indirme"
            category = category_for(name)
            directory = rec.get("directory")
            custom = (rec.get("options") or {}).get("custom_dir")
            ruled = None if custom else self.rule_directory(name, rec["url"])
            if ruled:
                directory = ruled
            elif not directory or (self.settings.get("category_subfolders") and not custom and rec.get("category") != category):
                directory = str(self.default_directory(category))
            base = sanitize_filename(name)
            stem, ext = os.path.splitext(base)
            opts = dict(rec.get("options") or {})
            own = opts.get("own_folder")
            if own is None:
                own = bool(self.settings.get("video_own_folder")) and category == "video"
            target_dir = os.path.join(directory, sanitize_filename(stem)) if own and category == "video" else directory
            if own and category == "video":
                opts["own_folder"] = True
                opts["own_dir"] = target_dir
                rec["options"] = opts
            Path(target_dir).mkdir(parents=True, exist_ok=True)
            taken = {r.get("path") for r in self.items.values() if r["id"] != did and r["status"] != "done"}
            path = unique_path(target_dir, base)
            n = 1
            while str(path) in taken:
                path = unique_path(target_dir, f"{stem} [{n}]{ext}")
                n += 1
            Path(str(path) + ".part").touch()
            rec.update(category=category, directory=directory, path=str(path))
        self.store.update(did, category=category, directory=directory, path=str(path), options=rec.get("options"))
        return path

    def _persist(self, did, state):
        now = time.monotonic()
        fields = {k: state[k] for k in ("segments", "downloaded", "total", "resumable", "etag", "last_modified") if k in state}
        with self.lock:
            rec = self.items.get(did)
            if rec:
                rec.update(fields)
        self.store.update(did, **fields)
        self._last_persist[did] = now

    def _on_info(self, did, info):
        fields = {}
        if info.get("path"):
            fields["path"] = info["path"]
        if info.get("filename"):
            fields["filename"] = info["filename"]
        if "total" in info:
            fields["total"] = info["total"]
        if "resumable" in info:
            fields["resumable"] = info["resumable"]
        self._update(did, **fields)
        hashes = info.get("hashes")
        if hashes and not ((self.get(did) or {}).get("vt") or {}).get("expected"):
            if self.vt.enabled():
                self.vt.check_hash(did, hashes)
            else:
                self._vt_patch(did, {"expected": hashes})

    def _make_job(self, rec):
        opts = rec.get("options") or {}
        if rec["kind"] == "video":
            return VideoDownloader(rec, self.settings,
                                   rate_limit=int(self.settings.get("speed_limit_kib") or 0) * 1024,
                                   on_info=lambda info: self._on_info(rec["id"], info))
        return SegmentedDownloader(
            rec,
            connections=opts.get("connections") or self.settings.get("connections"),
            retries=self.settings.get("retries"),
            timeout=self.settings.get("timeout"),
            user_agent=(rec.get("headers") or {}).get("User-Agent") or self.settings.get("user_agent"),
            limiter=self.limiter,
            resolve_path=lambda info: self._resolve_path(rec["id"], info),
            on_persist=lambda st: self._persist(rec["id"], st),
            on_info=lambda info: self._on_info(rec["id"], info),
            interfaces=self._lane_interfaces(),
            multi_min=int(self.settings.get("multi_iface_min_mb") or 0) * 1024 * 1024,
        )

    def _lane_interfaces(self):
        if not self.settings.get("multi_iface"):
            return None
        names = [n for n in (self.settings.get("multi_ifaces") or []) if iface_exists(n)]
        return names if len(names) >= 2 else None

    def _run(self, did):
        with self.lock:
            rec = dict(self.items[did])
        try:
            job = self._make_job(rec)
        except Exception as e:
            self._update(did, status="error", error=str(e))
            self.schedule()
            return
        with self.lock:
            self.jobs[did] = job
        started = time.monotonic()
        try:
            status, detail = job.run()
        except Exception as e:
            status, detail = "error", str(e) or e.__class__.__name__
        elapsed = (rec.get("elapsed") or 0) + (time.monotonic() - started)
        with self.lock:
            self.jobs.pop(did, None)
            current = self.items.get(did)
            wanted = current.get("_wanted") if current else None
            if current:
                current.pop("_wanted", None)
        if current is None:
            self.schedule()
            return
        if wanted == "cancel":
            self._discard_files(did, job)
            self.schedule()
            return
        code = getattr(job, "fatal_code", None)
        opts = dict((current or {}).get("options") or {})
        if status != "error" or code != "different":
            opts.pop("refreshed", None)
        if status in ("done", "paused"):
            opts.pop("expired", None)
        if status == "error" and code == "expired" and job.downloaded() > 0 and getattr(job, "ranges", False):
            opts["expired"] = True
            detail = f"Link süresi dolmuş olabilir — yeni linkle kaldığı yerden sürdürülebilir ({detail})"
        if status == "error" and code == "different":
            opts.pop("refreshed", None)
            opts["expired"] = True
        self._update(did, emit=False, options=opts)
        if status == "done":
            fields = {"status": "done", "finished": time.time(), "error": None, "segments": None, "elapsed": elapsed}
            if detail:
                fields["path"] = detail
                fields["filename"] = os.path.basename(detail)
                try:
                    size = os.path.getsize(detail)
                    fields["total"] = size
                    fields["downloaded"] = size
                except OSError:
                    pass
            snap = self._update(did, **fields)
            self._emit("finished", snap)
            if snap.get("path") and os.path.exists(snap["path"]):
                self.vt.check_file(did, snap["path"], (snap.get("vt") or {}).get("expected"))
                self.post.start(did)
        elif status == "paused":
            snap = self._update(did, status="queued" if wanted == "requeue" else "paused",
                                downloaded=job.downloaded(), elapsed=elapsed)
        else:
            snap = self._update(did, status="error", error=detail, downloaded=job.downloaded(), elapsed=elapsed)
            self._emit("failed", snap)
        self.schedule()

    def _vt_patch(self, did, patch):
        alert = None
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return
            vt = dict(rec.get("vt") or {})
            before_url = vt.get("url_verdict")
            before_file = vt.get("verdict")
            vt.update(patch)
            if "error" not in patch and patch.get("state") not in (None, "error") and patch.get("url_state") is None:
                vt.pop("error", None)
            rec["vt"] = vt
            fields = {"vt": vt}
            if patch.get("sha256"):
                rec["sha256"] = patch["sha256"]
                fields["sha256"] = patch["sha256"]
            if vt.get("url_verdict") == "malicious" and before_url != "malicious" and not vt.get("ack") \
                    and rec["status"] in ACTIVE_STATES:
                alert = "url"
            elif vt.get("verdict") == "malicious" and before_file != "malicious":
                alert = "file"
            snap = dict(rec)
        self.store.update(did, **fields)
        if alert == "url":
            self.pause(did)
            snap["_alert"] = "url"
            self._emit("vt_alert", snap)
        elif alert == "file":
            snap["_alert"] = "file"
            self._emit("vt_alert", snap)
        self._emit("updated", snap)

    @staticmethod
    def _norm_name(name):
        base = os.path.basename(name or "").casefold().strip()
        stem, ext = os.path.splitext(base)
        stem = re.sub(r"\s*[\(\[]\d+[\)\]]$", "", stem)
        return stem + ext

    def find_resume_candidate(self, filename, size=None, exclude=None):
        key = self._norm_name(filename)
        if not key:
            return None
        best = None
        with self.lock:
            for r in self.items.values():
                if r["id"] == exclude or r["status"] in ("running", "queued", "done"):
                    continue
                if r.get("kind") != "file" or not r.get("resumable") or not (r.get("downloaded") or 0):
                    continue
                names = {self._norm_name(r.get("filename")), self._norm_name(r.get("path")),
                         self._norm_name(filename_from_url(r["url"])),
                         self._norm_name((r.get("options") or {}).get("filename"))}
                names.discard("")
                if key not in names:
                    continue
                if size and r.get("total") and int(size) != int(r["total"]):
                    continue
                if best is None or (r.get("created") or 0) > (best.get("created") or 0):
                    best = r
            return dict(best) if best else None

    def refresh_url(self, did, url, headers=None, page_url=None):
        with self.lock:
            rec = self.items.get(did)
            if not rec or rec["status"] in ("running", "queued", "done"):
                return False
            merged = dict(rec.get("headers") or {})
            for k in ("Cookie", "Referer", "User-Agent"):
                merged.pop(k, None)
            merged.update(headers or {})
            opts = dict(rec.get("options") or {})
            opts["refreshed"] = True
            opts.pop("expired", None)
            vt = dict(rec.get("vt") or {})
            for k in ("url_state", "url_verdict", "url_stats", "url_scope", "url_link", "ack"):
                vt.pop(k, None)
            fields = {"url": url, "headers": merged, "options": opts, "vt": vt, "error": None}
            if page_url:
                fields["page_url"] = page_url
            rec.update(fields)
        self.store.update(did, **fields)
        with self.lock:
            self.items[did]["status"] = "paused"
        self.resume(did)
        return True

    def vt_acknowledge(self, did):
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return
            vt = dict(rec.get("vt") or {})
            vt["ack"] = True
            rec["vt"] = vt
        self.store.update(did, vt=vt)
        self.resume(did)

    def vt_recheck(self, did):
        rec = self.get(did)
        if not rec or rec["status"] != "done" or not rec.get("path") or not os.path.exists(rec["path"]):
            return False
        self.vt.check_file(did, rec["path"], (rec.get("vt") or {}).get("expected"))
        return True

    def vt_upload(self, did):
        rec = self.get(did)
        if not rec or rec["status"] != "done" or not rec.get("path") or not os.path.exists(rec["path"]):
            return False
        if not rec.get("sha256"):
            return False
        self.vt.upload(did, rec["path"], rec["sha256"])
        return True

    def progress(self):
        with self.lock:
            jobs = dict(self.jobs)
        out = {}
        for did, job in jobs.items():
            try:
                out[did] = job.snapshot()
            except Exception:
                pass
        return out

    def snapshot(self, did):
        with self.lock:
            job = self.jobs.get(did)
        if job:
            return job.snapshot()
        rec = self.get(did)
        if not rec:
            return None
        segs = [{"idx": i, "start": s, "end": e, "pos": p, "active": False, "state": "duraklatıldı"}
                for i, (s, e, p) in enumerate(rec.get("segments") or [])]
        if rec["status"] == "done" and rec.get("total"):
            segs = [{"idx": 0, "start": 0, "end": rec["total"] - 1, "pos": rec["total"], "active": False, "state": "tamam"}]
        return {"downloaded": rec.get("downloaded") or 0, "total": rec.get("total"), "speed": 0.0,
                "segments": segs, "connections": 0, "resumable": rec.get("resumable")}

    def pause(self, did):
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return
            job = self.jobs.get(did)
            if rec["status"] == "queued":
                rec["status"] = "paused"
                job = None
                snap = dict(rec)
            else:
                snap = None
        if snap:
            self.store.update(did, status="paused")
            self._emit("updated", snap)
        if job:
            job.stop()

    def resume(self, did):
        with self.lock:
            rec = self.items.get(did)
            if not rec or rec["status"] in ("running", "queued"):
                return
            if rec["status"] == "done":
                return
            rec["status"] = "queued"
            rec["error"] = None
            snap = dict(rec)
        self.store.update(did, status="queued", error=None)
        self._emit("updated", snap)
        self.schedule()

    def restart(self, did):
        with self.lock:
            rec = self.items.get(did)
            if not rec or rec["status"] in ("running",):
                return
        self._remove_partial(rec)
        self._update(did, segments=None, downloaded=0, path=None, total=None, resumable=False, vt=None, sha256=None, elapsed=0, post=None,
                     emit=False)
        with self.lock:
            self.items[did]["status"] = "paused"
        self.resume(did)

    def pause_all(self):
        for r in self.records():
            if r["status"] in ACTIVE_STATES:
                self.pause(r["id"])

    def resume_all(self):
        for r in self.records():
            if r["status"] in ("paused", "error"):
                self.resume(r["id"])

    def _remove_partial(self, rec):
        if rec.get("path"):
            try:
                os.unlink(rec["path"] + ".part")
            except OSError:
                pass
        if rec.get("kind") == "video":
            shutil.rmtree(TEMP_DIR / f"v-{rec['id']}", ignore_errors=True)
        own_dir = (rec.get("options") or {}).get("own_dir")
        if not own_dir and (rec.get("options") or {}).get("own_folder") and rec.get("path"):
            own_dir = os.path.dirname(rec["path"])
        if own_dir and rec.get("status") != "done":
            try:
                os.rmdir(own_dir)
            except OSError:
                pass

    def _discard_files(self, did, job=None):
        with self.lock:
            rec = self.items.pop(did, None)
        if not rec:
            return
        self._remove_partial(rec)
        if rec.get("_delete_file") and rec.get("path") and rec["status"] == "done":
            try:
                os.unlink(rec["path"])
            except OSError:
                pass
        self.store.delete(did)
        self._emit("removed", rec)

    def remove(self, did, delete_file=False):
        with self.lock:
            rec = self.items.get(did)
            if not rec:
                return
            job = self.jobs.get(did)
            rec["_delete_file"] = delete_file
            if job:
                rec["_wanted"] = "cancel"
        if job:
            job.stop()
            return
        if delete_file and rec["status"] == "done" and rec.get("path"):
            try:
                os.unlink(rec["path"])
            except OSError:
                pass
        self._discard_files(did)

    def clear_finished(self):
        for r in self.records():
            if r["status"] == "done":
                self.remove(r["id"])

    def move(self, did, delta):
        with self.lock:
            ordered = sorted(self.items.values(), key=lambda r: (r.get("queue_pos") or 0, r.get("created") or 0))
            idx = next((i for i, r in enumerate(ordered) if r["id"] == did), None)
            if idx is None:
                return
            j = max(0, min(len(ordered) - 1, idx + delta))
            ordered.insert(j, ordered.pop(idx))
            for i, r in enumerate(ordered):
                r["queue_pos"] = i + 1
        for r in ordered:
            self.store.update(r["id"], queue_pos=r["queue_pos"])

    def active_count(self):
        with self.lock:
            return sum(1 for r in self.items.values() if r["status"] in ACTIVE_STATES)

    def shutdown(self, timeout=8.0):
        self.shutting_down = True
        self.vt.stop()
        with self.lock:
            jobs = list(self.jobs.items())
            for did, _ in jobs:
                self.items[did]["_wanted"] = "requeue"
        for _, job in jobs:
            job.stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not self.jobs:
                    break
            time.sleep(0.1)
        with self.lock:
            for did, _ in jobs:
                rec = self.items.get(did)
                if rec and rec["status"] in ("running", "paused"):
                    rec["status"] = "queued"
                    self.store.update(did, status="queued")
