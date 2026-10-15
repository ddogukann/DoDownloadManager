import base64
import binascii
import mimetypes
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .netlanes import bound_session
from .util import filename_from_disposition, filename_from_url, unique_path

CHUNK = 64 * 1024
EXPIRED_CODES = (401, 403, 404, 410)
LANE_MIN_STEAL = 256 * 1024
LANE_STALL = 6.0
MIN_SEGMENT = 1024 * 1024
MIN_STEAL = 2 * MIN_SEGMENT
PERSIST_INTERVAL = 2.0


class RateLimiter:
    def __init__(self, rate=0):
        self._lock = threading.Lock()
        self.rate = int(rate or 0)
        self._allowance = 0.0
        self._last = time.monotonic()

    def set_rate(self, rate):
        with self._lock:
            self.rate = max(0, int(rate or 0))
            self._allowance = 0.0
            self._last = time.monotonic()

    def consume(self, n, stop=None):
        with self._lock:
            rate = self.rate
            if rate <= 0:
                return
            now = time.monotonic()
            self._allowance = min(float(rate), self._allowance + (now - self._last) * rate)
            self._last = now
            self._allowance -= n
            wait = -self._allowance / rate if self._allowance < 0 else 0.0
        if wait > 0:
            if stop is not None:
                stop.wait(wait)
            else:
                time.sleep(wait)


class SpeedMeter:
    def __init__(self, window=3.0):
        self.window = window
        self._lock = threading.Lock()
        self._samples = deque()
        self._total = 0

    def add(self, n):
        now = time.monotonic()
        with self._lock:
            self._total += n
            self._samples.append((now, self._total))
            cutoff = now - self.window - 1
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def speed(self):
        now = time.monotonic()
        with self._lock:
            while len(self._samples) > 1 and self._samples[0][0] < now - self.window:
                self._samples.popleft()
            if not self._samples:
                return 0.0
            t0, b0 = self._samples[0]
            elapsed = max(now - t0, 0.5)
            return max(0.0, (self._total - b0) / elapsed) if now - self._samples[-1][0] < self.window else 0.0


class DownloadError(Exception):
    pass


class Throttled(Exception):
    def __init__(self, retry_after=None):
        super().__init__("throttled")
        self.retry_after = retry_after


class RangeIgnored(Exception):
    pass


def describe_error(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        messages = {
            401: "Yetki gerekli (401)",
            403: "Erişim reddedildi (403) — link süresi dolmuş olabilir",
            404: "Dosya bulunamadı (404)",
            410: "Link artık geçerli değil (410)",
            416: "Sunucu istenen aralığı kabul etmedi (416)",
        }
        return messages.get(code, f"Sunucu hatası ({code})")
    if isinstance(exc, (requests.Timeout, urllib3.exceptions.TimeoutError)):
        return "Zaman aşımı"
    if isinstance(exc, (requests.ConnectionError, urllib3.exceptions.ProtocolError)):
        return "Bağlantı hatası"
    if isinstance(exc, OSError) and exc.errno == 28:
        return "Diskte yer kalmadı"
    if isinstance(exc, PermissionError):
        return "Klasöre yazma izni yok"
    return str(exc) or exc.__class__.__name__


class Segment:
    __slots__ = ("idx", "start", "end", "pos", "active", "state", "errors", "lane")

    def __init__(self, idx, start, end, pos=None):
        self.idx = idx
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos
        self.active = False
        self.state = "bekliyor"
        self.errors = 0
        self.lane = None

    def remaining(self):
        if self.end is None:
            return -1
        return max(0, self.end + 1 - self.pos)

    def complete(self):
        return self.end is not None and self.pos > self.end

    def done_bytes(self):
        if self.end is None:
            return self.pos - self.start
        return max(0, min(self.pos, self.end + 1) - self.start)


class SegmentedDownloader:
    def __init__(self, record, *, connections=8, retries=8, timeout=30, user_agent=None,
                 limiter=None, resolve_path=None, on_persist=None, on_info=None, interfaces=None, multi_min=0):
        self.record = record
        self.url = record["url"]
        self.fetch_url = self.url
        self.headers = dict(record.get("headers") or {})
        self.connections = max(1, min(32, int(connections)))
        self.retries = max(0, int(retries))
        self.timeout = max(5, int(timeout))
        self.user_agent = user_agent
        self.limiter = limiter or RateLimiter(0)
        self.resolve_path = resolve_path
        self.on_persist = on_persist
        self.on_info = on_info
        self.meter = SpeedMeter()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.segments = []
        self.total = record.get("total")
        self.ranges = bool(record.get("resumable"))
        self.etag = record.get("etag")
        self.last_modified = record.get("last_modified")
        self.path = Path(record["path"]) if record.get("path") else None
        self.allowed = self.connections
        self.last_throttle = 0.0
        self.active_workers = 0
        self.fatal = None
        self.fatal_code = None
        self.expired_hits = 0
        self.refreshed = bool((record.get("options") or {}).get("refreshed"))
        self.range_broken = False
        self._fd = None
        self._session = None
        self.lane_names = [n for n in (interfaces or []) if n]
        self.multi_min = int(multi_min or 0)
        self.lanes = []
        self._next_idx = 0

    def stop(self):
        self.stop_event.set()

    def _base_headers(self):
        h = {"Accept-Encoding": "identity", "Accept": "*/*"}
        if self.user_agent:
            h["User-Agent"] = self.user_agent
        for k, v in self.headers.items():
            if v:
                h[k] = v
        return h

    def _make_session(self):
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=self.connections + 4, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    def _probe(self):
        h = self._base_headers()
        h["Range"] = "bytes=0-0"
        r = self._session.get(self.url, headers=h, stream=True, timeout=(15, self.timeout), allow_redirects=True)
        try:
            if r.status_code in (429, 503):
                raise Throttled(_retry_after(r))
            info = {"final_url": r.url, "etag": r.headers.get("ETag"),
                    "last_modified": r.headers.get("Last-Modified"),
                    "content_type": (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()}
            cr = r.headers.get("Content-Range", "")
            if r.status_code == 206:
                m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", cr)
                info["ranges"] = True
                info["total"] = int(m.group(1)) if m and m.group(1) != "*" else None
                if info["total"] is None:
                    info["ranges"] = False
            elif r.status_code == 200:
                info["ranges"] = False
                cl = r.headers.get("Content-Length")
                info["total"] = int(cl) if cl and cl.isdigit() and not r.headers.get("Content-Encoding") else None
            elif r.status_code == 416:
                m = re.match(r"bytes\s+\*/(\d+)", cr)
                if m and int(m.group(1)) == 0:
                    info["ranges"] = False
                    info["total"] = 0
                else:
                    r.raise_for_status()
            else:
                r.raise_for_status()
                raise DownloadError(f"Beklenmeyen yanıt ({r.status_code})")
            name = filename_from_disposition(r.headers.get("Content-Disposition"))
            if not name:
                name = filename_from_url(r.url) or filename_from_url(self.url)
            if name and "." not in name and info["content_type"]:
                ext = mimetypes.guess_extension(info["content_type"])
                if ext and ext not in (".bin", ".html", ".htm"):
                    name += ext
            info["filename"] = name or "indirme"
            info["hashes"] = hashes_from_headers(r.headers, r.status_code)
            if "text/html" in info["content_type"] and not info.get("ranges") and (info.get("total") or 0) < 2 * 1024 * 1024:
                info["is_html"] = True
            return info
        finally:
            r.close()

    def _build_segments(self, saved):
        self.segments = []
        if saved and self.ranges and self.total:
            for i, (s, e, p) in enumerate(saved):
                self.segments.append(Segment(i, s, e, p))
            self._next_idx = len(self.segments)
            return
        if self.ranges and self.total:
            count = max(1, min(self.connections, self.total // MIN_SEGMENT or 1))
            size = self.total // count
            start = 0
            for i in range(count):
                end = self.total - 1 if i == count - 1 else start + size - 1
                self.segments.append(Segment(i, start, end))
                start = end + 1
            self._next_idx = count
        else:
            end = self.total - 1 if self.total else None
            self.segments = [Segment(0, 0, end)]
            self._next_idx = 1

    def downloaded(self):
        with self.lock:
            return sum(s.done_bytes() for s in self.segments)

    def snapshot(self):
        with self.lock:
            segs = [{"idx": s.idx, "start": s.start, "end": s.end, "pos": s.pos, "active": s.active,
                     "state": s.state, "lane": s.lane} for s in sorted(self.segments, key=lambda x: x.start)]
            done = sum(s.done_bytes() for s in self.segments)
            lanes = [{"name": l["name"], "speed": l["meter"].speed(), "dead": l["dead"], "workers": l["workers"]}
                     for l in self.lanes]
        return {"downloaded": done, "total": self.total, "speed": self.meter.speed(),
                "segments": segs, "connections": self.active_workers, "allowed": self.allowed,
                "resumable": self.ranges, "lanes": lanes}

    def _persist(self):
        if not self.on_persist:
            return
        with self.lock:
            saved = [[s.start, s.end, s.pos] for s in self.segments] if self.ranges else None
            done = sum(s.done_bytes() for s in self.segments)
        self.on_persist({"segments": saved, "downloaded": done, "total": self.total,
                         "resumable": self.ranges, "etag": self.etag, "last_modified": self.last_modified})

    def _next_segment(self):
        with self.lock:
            if self.fatal or self.range_broken:
                return None
            for s in sorted(self.segments, key=lambda x: x.start):
                if not s.active and not s.complete() and s.errors <= self.retries:
                    s.active = True
                    s.state = "bağlanıyor"
                    return s
            if not self.ranges:
                return None
            dead = {l["name"] for l in self.lanes if l["dead"]}
            for s in self.segments:
                if s.active and s.lane in dead and s.remaining() > 0:
                    new = Segment(self._next_idx, s.pos, s.end)
                    self._next_idx += 1
                    s.end = s.pos - 1
                    new.active = True
                    new.state = "devralındı"
                    self.segments.append(new)
                    return new
            min_steal = LANE_MIN_STEAL if self.lanes else MIN_STEAL
            victim = None
            for s in self.segments:
                if s.active and s.remaining() >= min_steal and (victim is None or s.remaining() > victim.remaining()):
                    victim = s
            if victim is None:
                return None
            mid = victim.pos + victim.remaining() // 2
            new = Segment(self._next_idx, mid, victim.end)
            self._next_idx += 1
            victim.end = mid - 1
            new.active = True
            new.state = "bağlanıyor"
            self.segments.append(new)
            return new

    def _worker(self, lane=None):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    if self.active_workers > self.allowed:
                        return
                    if lane is not None and lane["dead"]:
                        return
                seg = self._next_segment()
                if seg is None:
                    return
                with self.lock:
                    seg.lane = lane["name"] if lane is not None else None
                try:
                    self._download_segment(seg, lane)
                except Exception as e:
                    with self.lock:
                        self.fatal = describe_error(e)
                        seg.state = "hata"
                    self.stop_event.set()
                finally:
                    with self.lock:
                        seg.active = False
                        if seg.complete():
                            seg.state = "tamam"
                        elif seg.state not in ("hata",):
                            seg.state = "duraklatıldı" if self.stop_event.is_set() else "bekliyor"
        finally:
            with self.lock:
                self.active_workers -= 1
                if lane is not None:
                    lane["workers"] -= 1

    def _lane_failed(self, lane, exc):
        if lane is None:
            return False
        net = isinstance(exc, (requests.ConnectionError, requests.Timeout, urllib3.exceptions.ProtocolError,
                               urllib3.exceptions.TimeoutError, urllib3.exceptions.NewConnectionError))
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (19, 100, 101, 113):
            net = True
        if not net:
            return False
        with self.lock:
            lane["fails"] += 1
            others = [l for l in self.lanes if l is not lane and not l["dead"]]
            if lane["fails"] >= 3 and others:
                lane["dead"] = True
                return True
        return False

    def _download_segment(self, seg, lane=None):
        session = lane["session"] if lane is not None else self._session
        attempt = 0
        while not self.stop_event.is_set():
            with self.lock:
                if seg.complete():
                    return
                pos, end = seg.pos, seg.end
            h = self._base_headers()
            if self.ranges:
                h["Range"] = f"bytes={pos}-{end}" if end is not None else f"bytes={pos}-"
            elif pos:
                with self.lock:
                    seg.pos = 0
                pos = 0
            try:
                with session.get(self.fetch_url, headers=h, stream=True,
                                 timeout=(8, 12) if lane is not None else (15, self.timeout),
                                 allow_redirects=True) as r:
                    if r.status_code in (429, 503):
                        raise Throttled(_retry_after(r))
                    if self.ranges:
                        if r.status_code == 200:
                            raise RangeIgnored()
                        r.raise_for_status()
                        m = re.match(r"bytes\s+(\d+)-", r.headers.get("Content-Range", ""))
                        if r.status_code != 206 or not m or int(m.group(1)) != pos:
                            raise DownloadError("Sunucu yanlış aralık döndürdü")
                    else:
                        r.raise_for_status()
                    with self.lock:
                        seg.state = "indiriyor"
                        if lane is not None:
                            lane["fails"] = 0
                    for chunk in r.raw.stream(CHUNK, decode_content=False):
                        if self.stop_event.is_set():
                            return
                        if not chunk:
                            continue
                        with self.lock:
                            cur_end = seg.end
                            wpos = seg.pos
                            if cur_end is not None and wpos > cur_end:
                                break
                            if cur_end is not None and wpos + len(chunk) - 1 > cur_end:
                                chunk = chunk[: cur_end - wpos + 1]
                        os.pwrite(self._fd, chunk, wpos)
                        n = len(chunk)
                        with self.lock:
                            seg.pos = wpos + n if seg.end is None else min(wpos + n, seg.end + 1)
                            stop_now = seg.end is not None and seg.pos > seg.end
                        self.meter.add(n)
                        if lane is not None:
                            lane["meter"].add(n)
                            lane["last"] = time.monotonic()
                        self.limiter.consume(n, self.stop_event)
                        if stop_now:
                            break
                    with self.lock:
                        if seg.end is None and not self.stop_event.is_set():
                            seg.end = seg.pos - 1
                            self.total = seg.pos
                        if seg.complete():
                            return
                    attempt += 1
                    if attempt > self.retries:
                        raise DownloadError("Bağlantı sürekli erken kapandı")
                    continue
            except RangeIgnored:
                with self.lock:
                    self.range_broken = True
                self.stop_event.set()
                return
            except Throttled as t:
                with self.lock:
                    seg.state = "bekletiliyor"
                    self.last_throttle = time.monotonic()
                    if self.allowed > 1:
                        self.allowed = max(1, self.active_workers - 1)
                    leave = self.active_workers > self.allowed
                wait = t.retry_after if t.retry_after else min(30.0, 2.0 * (attempt + 1))
                attempt += 1
                if attempt > self.retries * 2:
                    with self.lock:
                        seg.state = "hata"
                        self.fatal = "Sunucu sürekli yoğun (429/503)"
                    return
                if leave:
                    return
                self.stop_event.wait(wait)
                continue
            except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, DownloadError) as e:
                if self._lane_failed(lane, e):
                    with self.lock:
                        seg.state = "bekliyor"
                    return
                if isinstance(e, OSError) and not isinstance(e, requests.RequestException) and e.errno in (28, 13, 30):
                    with self.lock:
                        self.fatal = describe_error(e)
                        seg.state = "hata"
                    self.stop_event.set()
                    return
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code in EXPIRED_CODES:
                    if self.fetch_url != self.url:
                        self.fetch_url = self.url
                    else:
                        with self.lock:
                            self.expired_hits += 1
                            hits = self.expired_hits
                        if hits >= 3:
                            with self.lock:
                                self.fatal = describe_error(e)
                                self.fatal_code = "expired"
                                seg.state = "hata"
                            self.stop_event.set()
                            return
                        if self.stop_event.wait(1.0):
                            return
                        continue
                attempt += 1
                with self.lock:
                    seg.errors += 1
                    seg.state = "yeniden deneniyor"
                if attempt > self.retries:
                    with self.lock:
                        seg.state = "hata"
                        self.fatal = describe_error(e)
                    self.stop_event.set()
                    return
                self.stop_event.wait(min(30.0, 0.75 * (2 ** min(attempt, 6))))

    def _open_file(self, part, fresh):
        flags = os.O_RDWR | os.O_CREAT
        self._fd = os.open(part, flags, 0o644)
        if fresh:
            os.ftruncate(self._fd, 0)
            if self.total:
                try:
                    os.posix_fallocate(self._fd, 0, self.total)
                except OSError as e:
                    if e.errno == 28:
                        raise
                    os.ftruncate(self._fd, self.total)

    def run(self):
        self._session = self._make_session()
        try:
            return self._run()
        except Throttled:
            return "error", "Sunucu yoğun (429/503)"
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, DownloadError) as e:
            if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code in EXPIRED_CODES:
                self.fatal_code = "expired"
            return "error", describe_error(e)
        finally:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            self._session.close()
            for lane in self.lanes:
                lane["session"].close()

    def _run(self):
        saved = self.record.get("segments")
        info = None
        for attempt in range(4):
            try:
                info = self._probe()
                break
            except Throttled as t:
                if self.stop_event.wait(t.retry_after or 3 * (attempt + 1)):
                    return "paused", None
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 3:
                    raise
                if self.stop_event.wait(2 * (attempt + 1)):
                    return "paused", None
        if info is None:
            raise Throttled()
        if self.stop_event.is_set():
            return "paused", None
        if info.get("is_html") and not self.record.get("allow_html"):
            return "error", "Link bir dosya yerine web sayfası döndürüyor"
        self.fetch_url = info["final_url"] or self.url
        part_exists = self.path is not None and Path(str(self.path) + ".part").exists()
        if self.refreshed and part_exists and saved and self.ranges:
            if not info.get("ranges"):
                self.fatal_code = "different"
                return "error", "Yeni link parçalı indirmeyi desteklemiyor; kaldığı yerden sürdürülemez"
            if info.get("total") != self.total:
                self.fatal_code = "different"
                return "error", (f"Yeni linkteki dosyanın boyutu farklı ({info.get('total')} ≠ {self.total} bayt); "
                                 "başka bir dosya olabilir")
        same = (part_exists and saved and self.ranges and info.get("ranges")
                and info.get("total") == self.total
                and (self.refreshed or not self.etag or not info.get("etag") or self.etag == info.get("etag"))
                and (self.refreshed or not self.last_modified or not info.get("last_modified")
                     or self.last_modified == info.get("last_modified")))
        if not same:
            saved = None
            self.total = info.get("total")
            self.ranges = bool(info.get("ranges"))
            self.etag = info.get("etag")
            self.last_modified = info.get("last_modified")
            if self.path is None or not part_exists:
                self.path = Path(self.resolve_path(info)) if self.resolve_path else Path(info["filename"])
        if self.on_info:
            self.on_info({"path": str(self.path), "filename": self.path.name, "total": self.total,
                          "resumable": self.ranges, "hashes": info.get("hashes") or {}})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(self.path) + ".part")
        self._open_file(part, fresh=not same)
        self._build_segments(saved)
        if same and self.refreshed:
            self.etag = info.get("etag")
            self.last_modified = info.get("last_modified")
            if not self._verify_sample():
                self.fatal_code = "different"
                return "error", "Yeni link aynı dosyayı vermiyor (örnek baytlar uyuşmadı); kaldığı yerden sürdürülemez"
        if (len(self.lane_names) >= 2 and self.ranges and self.total
                and self.total >= self.multi_min and not self.lanes):
            for name in self.lane_names:
                self.lanes.append({"name": name, "session": bound_session(name, self.connections + 2),
                                   "dead": False, "fails": 0, "workers": 0, "meter": SpeedMeter(),
                                   "last": time.monotonic()})
        if self.total == 0:
            os.close(self._fd)
            self._fd = None
            return self._finish(part)
        result = self._run_workers()
        if result[0] == "range_broken":
            self.stop_event.clear()
            self.range_broken = False
            self.ranges = False
            os.ftruncate(self._fd, 0)
            self._build_segments(None)
            result = self._run_workers()
        self._persist()
        if result[0] != "done":
            return result
        os.fsync(self._fd)
        os.close(self._fd)
        self._fd = None
        return self._finish(part)

    def _verify_sample(self, size=16384):
        with self.lock:
            done = sorted((s for s in self.segments if s.done_bytes() >= 1024), key=lambda s: -s.done_bytes())
        if not done:
            return True
        seg = done[0]
        n = min(size, seg.done_bytes())
        offset = seg.start + max(0, (seg.done_bytes() - n) // 2)
        local = os.pread(self._fd, n, offset)
        h = self._base_headers()
        h["Range"] = f"bytes={offset}-{offset + n - 1}"
        with self._session.get(self.fetch_url, headers=h, timeout=(15, self.timeout), stream=True) as r:
            r.raise_for_status()
            if r.status_code != 206:
                return False
            remote = r.raw.read(n + 1, decode_content=False)
        return remote == local

    def _run_workers(self):
        with self.lock:
            self.allowed = self.connections if self.ranges else 1
        threads = []

        def spawn():
            with self.lock:
                self.active_workers += 1
                alive = [l for l in self.lanes if not l["dead"]]
                lane = min(alive, key=lambda l: l["workers"]) if alive else None
                if lane is not None:
                    lane["workers"] += 1
            t = threading.Thread(target=self._worker, args=(lane,), daemon=True)
            t.start()
            threads.append(t)

        initial = min(self.allowed, max(1, len(self.segments)))
        for _ in range(initial):
            spawn()
        last_persist = time.monotonic()
        while True:
            alive = [t for t in threads if t.is_alive()]
            threads[:] = alive
            if not alive:
                break
            time.sleep(0.25)
            if self.lanes:
                now = time.monotonic()
                with self.lock:
                    alive = [l for l in self.lanes if not l["dead"]]
                    for l in alive:
                        if (len(alive) > 1 and l["workers"] > 0 and now - l["last"] > LANE_STALL
                                and any(o is not l and now - o["last"] < 3 for o in alive)):
                            l["dead"] = True
                            alive = [x for x in alive if not x["dead"]]
            if self.ranges and not self.stop_event.is_set() and not self.fatal:
                with self.lock:
                    if (self.allowed < self.connections and self.last_throttle
                            and time.monotonic() - self.last_throttle > 10):
                        self.allowed += 1
                        self.last_throttle = time.monotonic()
                    want = self.allowed - self.active_workers
                    ms = LANE_MIN_STEAL if self.lanes else MIN_STEAL
                    dead = {l["name"] for l in self.lanes if l["dead"]}
                    stealable = any((not s.active and not s.complete())
                                    or (s.active and (s.remaining() >= ms or (s.lane in dead and s.remaining() > 0)))
                                    for s in self.segments)
                if want > 0 and stealable:
                    spawn()
            if time.monotonic() - last_persist >= PERSIST_INTERVAL:
                self._persist()
                last_persist = time.monotonic()
        if self.range_broken:
            return "range_broken", None
        if self.fatal:
            return "error", self.fatal
        if self.stop_event.is_set():
            return "paused", None
        with self.lock:
            incomplete = [s for s in self.segments if not s.complete()]
        if incomplete:
            return "error", "Bazı parçalar indirilemedi"
        return "done", None

    def _finish(self, part):
        final = self.path
        if final.exists():
            final = unique_path(final.parent, final.name)
            self.path = final
        os.replace(part, final)
        self._persist()
        return "done", str(final)


def _b64_hex(value, size):
    try:
        raw = base64.b64decode(value.strip().strip(":"), validate=False)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == size else None


def hashes_from_headers(headers, status):
    out = {}
    for name in ("Repr-Digest", "Digest"):
        value = headers.get(name)
        if not value:
            continue
        for part in value.split(","):
            if "=" not in part:
                continue
            algo, _, val = part.strip().partition("=")
            algo = algo.strip().lower()
            if algo in ("sha-256", "sha256") and "sha256" not in out:
                h = _b64_hex(val, 32)
                if h:
                    out["sha256"] = h
            elif algo == "md5" and "md5" not in out:
                h = _b64_hex(val, 16)
                if h:
                    out["md5"] = h
    goog = headers.get("x-goog-hash")
    if goog:
        for part in goog.split(","):
            algo, _, val = part.strip().partition("=")
            if algo.lower() == "md5" and "md5" not in out:
                h = _b64_hex(val, 16)
                if h:
                    out["md5"] = h
    amz = headers.get("x-amz-checksum-sha256")
    if amz and "sha256" not in out:
        h = _b64_hex(amz, 32)
        if h:
            out["sha256"] = h
    if status == 200 and headers.get("Content-MD5") and "md5" not in out:
        h = _b64_hex(headers["Content-MD5"], 16)
        if h:
            out["md5"] = h
    return out


def _retry_after(r):
    v = r.headers.get("Retry-After")
    if v and v.strip().isdigit():
        return min(120.0, float(v.strip()))
    return None


def probe(url, headers=None, user_agent=None, timeout=20):
    d = SegmentedDownloader({"url": url, "headers": headers or {}}, connections=1, timeout=timeout,
                            user_agent=user_agent)
    d._session = d._make_session()
    try:
        return d._probe()
    finally:
        d._session.close()
