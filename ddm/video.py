import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import requests

from .config import BIN_DIR, TEMP_DIR
from .engine import SpeedMeter
from .i18n import _

YTDLP_RELEASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
DENO_RELEASE = "https://github.com/denoland/deno/releases/latest/download/deno-{arch}-unknown-linux-gnu.zip"
FORMAT_MISSING = ("Requested format is not available", "Only images are available")


def _tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout or "")
    return tuple(int(x) for x in m.groups()) if m else None


def deno_binary():
    local = BIN_DIR / "deno"
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("deno")


def js_runtime():
    deno = deno_binary()
    if deno:
        version = _tool_version([deno, "--version"])
        if version and version >= (2, 3, 0):
            return "deno", deno, version
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        version = _tool_version([node, "--version"])
        if version and version >= (22, 0, 0):
            return "node", node, version
    qjs = shutil.which("qjs")
    if qjs:
        return "quickjs", qjs, None
    return None


def js_runtime_args():
    rt = js_runtime()
    return ["--js-runtimes", f"{rt[0]}:{rt[1]}"] if rt else []


def update_deno(progress=None):
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(platform.machine())
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    archive = BIN_DIR / "deno.zip.download"
    with requests.get(DENO_RELEASE.format(arch=arch), stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(archive, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    tmp = BIN_DIR / "deno.download"
    try:
        with zipfile.ZipFile(archive) as z, z.open("deno") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
    finally:
        archive.unlink(missing_ok=True)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, BIN_DIR / "deno")
    return js_runtime()


def ytdlp_binary(settings=None):
    custom = settings.get("ytdlp_path") if settings else ""
    if custom and os.access(custom, os.X_OK):
        return custom
    local = BIN_DIR / "yt-dlp"
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("yt-dlp")


def ytdlp_version(settings=None):
    exe = ytdlp_binary(settings)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def update_ytdlp(progress=None):
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / "yt-dlp"
    tmp = BIN_DIR / "yt-dlp.download"
    with requests.get(YTDLP_RELEASE, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, target)
    return ytdlp_version()


def write_cookie_file(path, cookies):
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies or []:
        domain = c.get("domain") or ""
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") or not c.get("hostOnly", False) else "FALSE"
        if include_sub == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        if c.get("httpOnly"):
            domain = "#HttpOnly_" + domain
        expires = int(c.get("expirationDate") or 0)
        lines.append("\t".join([
            domain, include_sub, c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE", str(expires),
            c.get("name") or "", c.get("value") or "",
        ]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _common_args(headers, cookie_file):
    args = []
    ua = (headers or {}).get("User-Agent")
    if ua:
        args += ["--user-agent", ua]
    ref = (headers or {}).get("Referer")
    if ref:
        args += ["--referer", ref]
    if cookie_file:
        args += ["--cookies", str(cookie_file)]
    return args


def probe_formats(url, settings, headers=None, cookies=None, timeout=90):
    exe = ytdlp_binary(settings)
    if not exe:
        raise RuntimeError(_("yt-dlp bulunamadı. Ayarlar ▸ Video bölümünden indirin."))
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = None
    if cookies:
        cookie_file = TEMP_DIR / f"probe-{os.getpid()}-{threading.get_ident()}.cookies"
        write_cookie_file(cookie_file, cookies)
    try:
        cmd = [exe, "-J", "--no-playlist", "--no-warnings", "--no-colors", *js_runtime_args(),
               *_common_args(headers, cookie_file), url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0 and cookie_file and any(s in out.stderr for s in FORMAT_MISSING):
            cmd = [exe, "-J", "--no-playlist", "--no-warnings", "--no-colors", *js_runtime_args(),
                   *_common_args(headers, None), url]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            if any(s in out.stderr for s in FORMAT_MISSING) and not js_runtime():
                raise RuntimeError(_("YouTube için JavaScript çalışma ortamı (deno) gerekli. Ayarlar ▸ Video bölümünden tek tıkla kurabilirsiniz."))
            err = [l for l in out.stderr.splitlines() if l.startswith("ERROR")]
            raise RuntimeError((err[-1] if err else out.stderr.strip()[-300:]).replace("ERROR: ", ""))
        info = json.loads(out.stdout)
    finally:
        if cookie_file:
            try:
                cookie_file.unlink()
            except OSError:
                pass
    heights = sorted({f.get("height") for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    has_audio = any(f.get("acodec") not in (None, "none") for f in info.get("formats") or [])
    return {
        "title": info.get("title") or "video",
        "id": info.get("id"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "heights": heights,
        "has_audio": has_audio,
        "extractor": info.get("extractor_key"),
        "is_live": bool(info.get("is_live")),
    }


def build_format_args(options):
    opt = options or {}
    container = opt.get("container") or "mp4"
    if opt.get("audio_only"):
        fmt = opt.get("audio_format") or "mp3"
        return ["-f", "ba/b", "-x", "--audio-format", fmt, "--audio-quality", "0"]
    height = int(opt.get("height") or 0)
    sort = f"res:{height}" if height else "res"
    if container == "mp4":
        sort += ",ext:mp4:m4a"
    return ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", container]


class VideoDownloader:
    def __init__(self, record, settings, *, rate_limit=0, on_info=None):
        self.record = record
        self.settings = settings
        self.rate_limit = int(rate_limit or 0)
        self.on_info = on_info
        self.stop_event = threading.Event()
        self.proc = None
        self.meter = SpeedMeter()
        self.lock = threading.Lock()
        self.parts = {}
        self.speed_reported = 0.0
        self.eta = None
        self.phase = "hazırlanıyor"
        self.final_path = None
        self.total = record.get("total")
        self.tmpdir = TEMP_DIR / f"v-{record['id']}"

    def stop(self):
        self.stop_event.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGINT)
            except OSError:
                pass
            for _try in range(50):
                if p.poll() is not None:
                    return
                time.sleep(0.1)
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def downloaded(self):
        with self.lock:
            return sum(d for d, _ in self.parts.values())

    def snapshot(self):
        with self.lock:
            done = sum(d for d, _ in self.parts.values())
            totals = [t for _, t in self.parts.values() if t]
            total = sum(totals) if totals else self.total
            parts = [{"idx": i, "format": k, "pos": d, "total": t} for i, (k, (d, t)) in enumerate(self.parts.items())]
        return {"downloaded": done, "total": total, "speed": self.speed_reported or self.meter.speed(),
                "eta": self.eta, "phase": self.phase, "parts": parts, "segments": [],
                "connections": int(self.settings.get("ytdlp_fragments") or 1), "resumable": True}

    def _command(self, exe, cookie_file):
        rec = self.record
        opts = rec.get("options") or {}
        directory = rec.get("directory")
        cmd = [exe, "--newline", "--no-colors", "--no-playlist", "--continue", "--progress", "--no-simulate",
               "--progress-template",
               "download:YLDP %(info.format_id)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
               "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.status)s",
               "--print", "before_dl:YLDT %(title)s",
               "--print", "after_move:YLDF %(filepath)s",
               "-N", str(int(self.settings.get("ytdlp_fragments") or 4)),
               "-P", f"home:{directory}", "-P", f"temp:{self.tmpdir}",
               "-o", opts.get("template") or (
                   "%(title).150B/%(title).150B.%(ext)s" if opts.get("own_folder") and not opts.get("audio_only")
                   else "%(title).180B [%(id)s].%(ext)s"),
               "--windows-filenames" if opts.get("safe_names") else "--no-windows-filenames",
               *build_format_args(opts),
               *js_runtime_args(),
               *_common_args(rec.get("headers"), cookie_file)]
        if self.rate_limit > 0:
            cmd += ["--limit-rate", str(self.rate_limit)]
        cmd.append(rec["url"])
        return cmd

    def _parse(self, line):
        if line.startswith("YLDP "):
            fields = line[5:].split("|")
            if len(fields) < 7:
                return
            fid, done, total, est, speed, eta, status = fields[:7]
            d = _num(done)
            t = _num(total) or _num(est)
            with self.lock:
                prev = self.parts.get(fid, (0, None))[0]
                self.parts[fid] = (d if d is not None else prev, t)
                self.phase = "indiriyor" if status == "downloading" else "birleştiriliyor" if status == "finished" else status
                self.speed_reported = _num(speed) or 0.0
                self.eta = _num(eta)
            if d is not None and d > prev:
                self.meter.add(d - prev)
        elif line.startswith("YLDT "):
            title = line[5:].strip()
            if self.on_info and title:
                self.on_info({"filename": title})
        elif line.startswith("YLDF "):
            self.final_path = line[5:].strip()
        elif "[Merger]" in line or "[ExtractAudio]" in line or "[VideoConvertor]" in line or "[FixupM" in line:
            with self.lock:
                self.phase = "birleştiriliyor"
                self.speed_reported = 0.0

    def run(self):
        exe = ytdlp_binary(self.settings)
        if not exe:
            return "error", _("yt-dlp bulunamadı. Ayarlar ▸ Video bölümünden indirin.")
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        cookie_file = None
        cookies = (self.record.get("options") or {}).get("cookies")
        if cookies:
            cookie_file = self.tmpdir / "cookies.txt"
            write_cookie_file(cookie_file, cookies)
        Path(self.record["directory"]).mkdir(parents=True, exist_ok=True)
        try:
            code, errors = self._attempt(exe, cookie_file)
            if (code != 0 and cookie_file and not self.stop_event.is_set()
                    and any(s in " ".join(errors) for s in FORMAT_MISSING)):
                code, errors = self._attempt(exe, None)
        except OSError as e:
            return "error", _("yt-dlp başlatılamadı: {error}").format(error=e)
        finally:
            if cookie_file:
                try:
                    cookie_file.unlink()
                except OSError:
                    pass
        if self.stop_event.is_set():
            return "paused", None
        if code == 0 and self.final_path:
            self.cleanup()
            return "done", self.final_path
        if code == 0:
            self.cleanup()
            return "done", None
        msg = errors[-1] if errors else _("yt-dlp çıkış kodu {code}").format(code=code)
        if "DRM" in msg:
            msg = _("Bu içerik DRM korumalı, indirilemez")
        elif any(s in msg for s in FORMAT_MISSING) and not js_runtime():
            msg = _("YouTube için JavaScript çalışma ortamı (deno) gerekli. Ayarlar ▸ Video bölümünden tek tıkla kurabilirsiniz.")
        return "error", msg

    def _attempt(self, exe, cookie_file):
        errors = []
        self.proc = subprocess.Popen(self._command(exe, cookie_file), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True, errors="replace")
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith("ERROR:"):
                errors.append(line[6:].strip())
            self._parse(line)
        return self.proc.wait(), errors


def _num(v):
    if v in (None, "", "NA", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None
