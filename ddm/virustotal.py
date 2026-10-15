import base64
import hashlib
import itertools
import os
import queue
import threading
import time
from urllib.parse import urlsplit

import requests

API_BASE = os.environ.get("DDM_VT_API_BASE", "https://www.virustotal.com/api/v3")
GUI_BASE = "https://www.virustotal.com/gui"
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"
SMALL_UPLOAD = 32 * 1024 * 1024
MAX_UPLOAD = 650 * 1024 * 1024
CACHE_TTL = 6 * 3600
BUSY_STATES = ("hashing", "queued", "checking", "uploading", "analyzing")


class VTError(Exception):
    pass


def summarize(stats):
    stats = stats or {}
    mal = int(stats.get("malicious") or 0)
    sus = int(stats.get("suspicious") or 0)
    total = mal + sus + int(stats.get("undetected") or 0) + int(stats.get("harmless") or 0)
    if total == 0:
        verdict = "unknown"
    elif mal >= 3:
        verdict = "malicious"
    elif mal or sus:
        verdict = "suspicious"
    else:
        verdict = "clean"
    return {"malicious": mal, "suspicious": sus, "total": total}, verdict


def file_link(digest):
    return f"{GUI_BASE}/file/{digest}"


def url_link(url):
    return f"{GUI_BASE}/url/{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


def domain_link(host):
    return f"{GUI_BASE}/domain/{host}"


def url_id(url):
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def hash_file(path, want_md5=False, stop=None):
    sha = hashlib.sha256()
    md5 = hashlib.md5() if want_md5 else None
    with open(path, "rb", buffering=0) as f:
        while True:
            if stop is not None and stop.is_set():
                return None, None
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            if md5 is not None:
                md5.update(chunk)
    return sha.hexdigest(), (md5.hexdigest() if md5 is not None else None)


class VTClient:
    def __init__(self, api_key, min_interval=15.5):
        self.api_key = api_key
        self.min_interval = min_interval
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._last = 0.0

    def _wait_slot(self, stop=None):
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            self._last = max(time.monotonic(), self._last + self.min_interval)
        if wait > 0:
            if stop is not None:
                stop.wait(wait)
            else:
                time.sleep(wait)

    def request(self, method, path_or_url, stop=None, attempts=6, **kw):
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        for attempt in range(attempts):
            self._wait_slot(stop)
            try:
                r = self.session.request(method, url, headers={"x-apikey": self.api_key, "accept": "application/json"},
                                         timeout=(15, 120), **kw)
            except requests.RequestException as e:
                if attempt == attempts - 1:
                    raise VTError("VirusTotal'a ulaşılamadı") from e
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                raise VTError("VirusTotal API anahtarı geçersiz")
            if r.status_code == 429:
                if attempt >= 4:
                    raise VTError("VirusTotal kotası doldu, daha sonra tekrar denenecek")
                if stop is not None:
                    stop.wait(60)
                else:
                    time.sleep(60)
                continue
            if r.status_code >= 500:
                time.sleep(10)
                continue
            if r.status_code >= 400:
                raise VTError(f"VirusTotal hatası ({r.status_code})")
            return r.json()
        raise VTError("VirusTotal yanıt vermedi")

    def file_report(self, digest):
        data = self.request("GET", f"/files/{digest}")
        if data is None:
            return None
        attrs = data.get("data", {}).get("attributes", {})
        stats, verdict = summarize(attrs.get("last_analysis_stats"))
        return {"stats": stats, "verdict": verdict, "sha256": attrs.get("sha256") or digest,
                "name": attrs.get("meaningful_name")}

    def url_report(self, url, domain_cache=None):
        data = self.request("GET", f"/urls/{url_id(url)}")
        if data is not None:
            stats, verdict = summarize(data.get("data", {}).get("attributes", {}).get("last_analysis_stats"))
            if verdict != "unknown":
                return {"stats": stats, "verdict": verdict, "scope": "link", "link": url_link(url)}
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return None
        if domain_cache is not None:
            hit = domain_cache.get(host)
            if hit is not None and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        data = self.request("GET", f"/domains/{host}")
        result = None
        if data is not None:
            stats, verdict = summarize(data.get("data", {}).get("attributes", {}).get("last_analysis_stats"))
            if verdict != "unknown":
                result = {"stats": stats, "verdict": verdict, "scope": "alan adı", "link": domain_link(host)}
        if domain_cache is not None:
            domain_cache[host] = (time.time(), result)
        return result

    def upload(self, path, stop=None):
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise VTError("VirusTotal 650 MB üzerindeki dosyaları kabul etmiyor")
        target = "/files"
        if size > SMALL_UPLOAD:
            data = self.request("GET", "/files/upload_url", stop)
            target = (data or {}).get("data")
            if not target:
                raise VTError("Yükleme adresi alınamadı")
        data = None
        last_error = None
        for _ in range(3):
            try:
                with open(path, "rb") as f:
                    data = self.request("POST", target, stop, attempts=1, files={"file": (os.path.basename(path), f)})
                break
            except VTError as e:
                last_error = e
                if "anahtarı" in str(e):
                    raise
        if data is None and last_error is not None:
            raise last_error
        analysis = (data or {}).get("data", {}).get("id")
        if not analysis:
            raise VTError("Yükleme kabul edilmedi")
        return analysis

    def analysis(self, analysis_id, stop=None):
        data = self.request("GET", f"/analyses/{analysis_id}", stop)
        attrs = (data or {}).get("data", {}).get("attributes", {})
        return attrs.get("status"), attrs.get("stats")


class VTService:
    def __init__(self, settings, on_patch, min_interval=15.5):
        self.settings = settings
        self.on_patch = on_patch
        self.min_interval = min_interval
        self.api_q = queue.PriorityQueue()
        self._seq = itertools.count()
        self.domain_cache = {}
        self.hash_q = queue.Queue()
        self.stop_event = threading.Event()
        self.cache = {}
        self._client = None
        self._client_key = None
        threading.Thread(target=self._api_loop, daemon=True, name="vt-api").start()
        threading.Thread(target=self._hash_loop, daemon=True, name="vt-hash").start()

    def enabled(self):
        return bool(self.settings.get("vt_enabled")) and bool((self.settings.get("vt_api_key") or "").strip())

    def client(self):
        key = (self.settings.get("vt_api_key") or "").strip()
        if self._client is None or self._client_key != key:
            self._client = VTClient(key, self.min_interval)
            self._client_key = key
        return self._client

    def stop(self):
        self.stop_event.set()
        self._put(-1, None)
        self.hash_q.put(None)

    def check_url(self, did, url):
        if not self.enabled():
            return
        self.on_patch(did, {"url_state": "checking"})
        self._put(0, ("url", did, url))

    def check_hash(self, did, hashes):
        if not self.enabled() or not hashes:
            return
        self.on_patch(did, {"expected": hashes, "state": "queued", "source": "sunucu"})
        self._put(1, ("hash", did, hashes.get("sha256") or hashes.get("md5"), "sunucu"))

    def check_file(self, did, path, expected=None):
        self.on_patch(did, {"state": "hashing"})
        self.hash_q.put((did, path, expected or {}))

    def upload(self, did, path, digest):
        if not self.enabled():
            return
        self.on_patch(did, {"state": "uploading", "error": None})
        self._put(3, ("upload", did, path, digest))

    def _put(self, priority, job):
        self.api_q.put((priority, next(self._seq), job))

    def test_key(self, key):
        client = VTClient(key.strip(), 0)
        report = client.file_report(EICAR_SHA256)
        return report is not None

    def _hash_loop(self):
        while not self.stop_event.is_set():
            job = self.hash_q.get()
            if job is None:
                return
            did, path, expected = job
            try:
                sha, md5 = hash_file(path, want_md5=bool(expected.get("md5")), stop=self.stop_event)
            except OSError as e:
                self.on_patch(did, {"state": "error", "error": f"Dosya okunamadı: {e.strerror or e}"})
                continue
            if sha is None:
                return
            patch = {"sha256": sha}
            mismatch = False
            if expected.get("sha256") and expected["sha256"].lower() != sha:
                mismatch = True
            if expected.get("md5") and md5 and expected["md5"].lower() != md5:
                mismatch = True
            patch["mismatch"] = mismatch
            if not self.enabled():
                patch["state"] = "disabled"
                self.on_patch(did, patch)
                continue
            cached = self.cache.get(sha)
            if cached is not None and time.time() - cached.get("checked", 0) > CACHE_TTL:
                cached = None
            if cached is not None:
                patch.update(cached)
                patch["source"] = "dosya"
                self.on_patch(did, patch)
                continue
            patch["state"] = "queued"
            self.on_patch(did, patch)
            self._put(2, ("hash", did, sha, "dosya"))

    def _api_loop(self):
        while not self.stop_event.is_set():
            _, _, job = self.api_q.get()
            if job is None:
                return
            kind, did = job[0], job[1]
            try:
                if kind == "url":
                    self._do_url(did, job[2])
                elif kind == "hash":
                    self._do_hash(did, job[2], job[3])
                elif kind == "upload":
                    self._do_upload(did, job[2], job[3])
            except VTError as e:
                key = "url_state" if kind == "url" else "state"
                self.on_patch(did, {key: "error", "error": str(e)})
            except Exception as e:
                key = "url_state" if kind == "url" else "state"
                self.on_patch(did, {key: "error", "error": str(e) or e.__class__.__name__})

    def _do_url(self, did, url):
        report = self.client().url_report(url, self.domain_cache)
        if report is None:
            self.on_patch(did, {"url_state": "unknown"})
            return
        self.on_patch(did, {"url_state": "done", "url_verdict": report["verdict"], "url_stats": report["stats"],
                            "url_scope": report["scope"], "url_link": report["link"]})

    def _do_hash(self, did, digest, source):
        self.on_patch(did, {"state": "checking"})
        report = self.client().file_report(digest)
        if report is None or report["verdict"] == "unknown":
            self.on_patch(did, {"state": "unknown", "source": source})
            return
        result = {"state": "done", "verdict": report["verdict"], "stats": report["stats"],
                  "link": file_link(report["sha256"]), "checked": time.time()}
        self.cache[report["sha256"]] = result
        self.on_patch(did, dict(result, source=source))

    def _do_upload(self, did, path, digest):
        client = self.client()
        analysis_id = client.upload(path, self.stop_event)
        self.on_patch(did, {"state": "analyzing"})
        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline and not self.stop_event.is_set():
            status, stats = client.analysis(analysis_id, self.stop_event)
            if status == "completed":
                summary, verdict = summarize(stats)
                result = {"state": "done", "verdict": verdict, "stats": summary, "link": file_link(digest),
                          "checked": time.time()}
                self.cache[digest] = result
                self.on_patch(did, dict(result, source="yükleme"))
                return
            self.stop_event.wait(max(20, self.min_interval))
        self.on_patch(did, {"state": "analyzing", "error": "Analiz uzun sürüyor; sonucu VirusTotal sayfasından izleyin",
                            "link": file_link(digest)})
