import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

from .i18n import _
from .util import unique_path
from .virustotal import BUSY_STATES

CHECKSUM_NAMES = ["SHA256SUMS", "SHA256SUMS.txt", "sha256sums.txt", "sha256sum.txt", "SHA256SUM", "checksums.txt",
                  "CHECKSUMS", "CHECKSUM"]
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
SEVENZ_SUFFIXES = (".7z", ".rar", ".tar.zst", ".tzst", ".zst")
ARCHIVE_SUFFIXES = (".zip",) + TAR_SUFFIXES + SEVENZ_SUFFIXES
HEX64 = re.compile(r"^[a-fA-F0-9]{64}$")


def is_archive(name):
    n = (name or "").lower()
    return n.endswith(ARCHIVE_SUFFIXES)


def archive_stem(name):
    n = name
    for suf in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if n.lower().endswith(suf):
            return n[: -len(suf)] or n
    return os.path.splitext(n)[0]


def parse_checksums(text, names):
    wanted = {n.casefold() for n in names if n}
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    for line in lines:
        m = re.match(r"^([a-fA-F0-9]{64})\s+\*?(.+)$", line)
        if m and os.path.basename(m.group(2).strip()).casefold() in wanted:
            return m.group(1).lower()
        m = re.match(r"^SHA256\s*\((.+)\)\s*=\s*([a-fA-F0-9]{64})$", line, re.I)
        if m and os.path.basename(m.group(1).strip()).casefold() in wanted:
            return m.group(2).lower()
    return None


def bare_hash(text):
    tokens = text.split()
    if tokens and HEX64.match(tokens[0]) and len(tokens) <= 2:
        return tokens[0].lower()
    return None


def find_checksum(url, names, headers=None, timeout=8):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    base_path = parts.path.rsplit("/", 1)[0] + "/"
    primary = unquote(parts.path.rsplit("/", 1)[-1])
    names = [n for n in dict.fromkeys([primary] + list(names)) if n]
    candidates = []
    for n in names:
        candidates += [f"{n}.sha256", f"{n}.sha256sum", f"{n}.sha256.txt"]
    candidates += CHECKSUM_NAMES
    session = requests.Session()
    h = {"Accept": "text/plain,*/*", "Accept-Encoding": "identity"}
    for k in ("User-Agent", "Referer", "Cookie"):
        if headers and headers.get(k):
            h[k] = headers[k]
    try:
        for cand in candidates:
            target = urlunsplit((parts.scheme, parts.netloc, base_path + cand, "", ""))
            try:
                with session.get(target, headers=h, timeout=(timeout, timeout), stream=True,
                                 allow_redirects=True) as r:
                    if r.status_code != 200:
                        continue
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "html" in ctype:
                        continue
                    body = r.raw.read(1024 * 1024 + 1, decode_content=True)
                    if len(body) > 1024 * 1024:
                        continue
            except requests.RequestException:
                continue
            text = body.decode("utf-8", "replace")
            digest = parse_checksums(text, names)
            if digest is None and cand.endswith((".sha256", ".sha256sum", ".sha256.txt")):
                digest = bare_hash(text)
            if digest:
                return {"sha256": digest, "source": target, "file": cand}
    finally:
        session.close()
    return None


def _safe_zip_extract(zf, dest):
    root = os.path.realpath(dest)
    for info in zf.infolist():
        target = os.path.realpath(os.path.join(dest, info.filename))
        if not (target == root or target.startswith(root + os.sep)):
            raise ValueError(_("Arşivde güvensiz yol: {name}").format(name=info.filename))
    zf.extractall(dest)


def extract_archive(path, stop=None):
    path = Path(path)
    parent = path.parent
    tmp = parent / f".ddm-extract-{os.getpid()}-{threading.get_ident()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    name = path.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                _safe_zip_extract(zf, tmp)
        elif name.endswith(TAR_SUFFIXES):
            with tarfile.open(path) as tf:
                tf.extractall(tmp, filter="data")
        elif name.endswith(SEVENZ_SUFFIXES):
            exe = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
            if not exe:
                raise RuntimeError(_("7z kurulu değil (sudo apt install 7zip)"))
            out = subprocess.run([exe, "x", "-y", f"-o{tmp}", str(path)], capture_output=True, text=True)
            if out.returncode != 0:
                raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[-1] if (out.stderr or out.stdout)
                                   else _("7z çıkış kodu {code}").format(code=out.returncode))
            if name.endswith((".tar.zst", ".tzst")):
                inner = [p for p in tmp.iterdir() if p.suffix == ".tar"]
                if len(inner) == 1:
                    with tarfile.open(inner[0]) as tf:
                        tf.extractall(tmp, filter="data")
                    inner[0].unlink()
        else:
            raise RuntimeError(_("Desteklenmeyen arşiv türü"))
        entries = list(tmp.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            dest = unique_path(parent, entries[0].name)
            shutil.move(str(entries[0]), str(dest))
            tmp.rmdir()
        else:
            dest = unique_path(parent, archive_stem(path.name))
            tmp.rename(dest)
        return str(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


class PostProcessor:
    def __init__(self, manager):
        self.manager = manager
        self.settings = manager.settings

    def start(self, did):
        threading.Thread(target=self._run, args=(did,), daemon=True, name=f"post-{did}").start()

    def _wait(self, did, cond, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rec = self.manager.get(did)
            if rec is None:
                return None
            if cond(rec):
                return rec
            time.sleep(1.0)
        return self.manager.get(did)

    def _run(self, did):
        rec = self.manager.get(did)
        if not rec or rec["status"] != "done" or not rec.get("path"):
            return
        rule = self.manager.rule_for(rec.get("filename") or os.path.basename(rec["path"]), rec["url"])
        post = {}
        if self.settings.get("checksum_auto") and rec.get("kind") == "file":
            self.manager.set_post(did, {"checksum": "searching"})
            names = [rec.get("filename"), os.path.basename(rec["path"])]
            found = None
            try:
                found = find_checksum(rec["url"], names, rec.get("headers"))
            except Exception:
                found = None
            if found:
                rec = self._wait(did, lambda r: r.get("sha256") or (r.get("vt") or {}).get("state") == "error", 30 * 60)
                if rec is None:
                    return
                sha = rec.get("sha256")
                if sha:
                    ok = sha.lower() == found["sha256"]
                    self.manager._vt_patch(did, {"expected": {"sha256": found["sha256"]}, "mismatch": not ok,
                                                 "checksum_src": found["source"], "checksum_file": found["file"]})
                    post["checksum"] = "ok" if ok else "bad"
                    if not ok:
                        self.manager.emit_alert(did, "checksum")
                else:
                    post["checksum"] = "error"
            else:
                post["checksum"] = "none"
            self.manager.set_post(did, post)
        if not rule or not (rule.get("extract") or rule.get("open")):
            return
        if self.manager.vt.enabled():
            rec = self._wait(did, lambda r: (r.get("vt") or {}).get("state") not in BUSY_STATES, 10 * 60)
            if rec is None:
                return
        rec = self.manager.get(did)
        vt = rec.get("vt") or {}
        if vt.get("verdict") == "malicious" or vt.get("mismatch"):
            self.manager.set_post(did, {"action": "skipped",
                                        "reason": _("Dosya güvenli görünmediği için otomatik işlem yapılmadı")})
            return
        path = rec.get("path")
        if rule.get("extract") and path and is_archive(path) and os.path.exists(path):
            self.manager.set_post(did, {"action": "extracting", "rule": rule.get("name")})
            try:
                dest = extract_archive(path)
                info = {"action": "extracted", "dest": dest, "rule": rule.get("name")}
                if rule.get("delete_archive"):
                    try:
                        os.unlink(path)
                        info["archive_deleted"] = True
                    except OSError:
                        pass
                self.manager.set_post(did, info)
                if rule.get("open"):
                    self.manager.emit_simple("post_open_path", did, dest)
            except Exception as e:
                self.manager.set_post(did, {"action": "error", "reason": _("Arşiv açılamadı: {error}").format(error=e)})
            return
        if rule.get("open") and path and os.path.exists(path):
            self.manager.set_post(did, {"action": "opened", "rule": rule.get("name")})
            self.manager.emit_simple("post_open", did, path)
