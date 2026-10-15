const tabMedia = new Map();
const tabUrls = new Map();
const handedOff = new Set();
let settings = Object.assign({}, DEFAULTS);
let appInfo = null;
let appCheckedAt = 0;

const MEDIA_EXTS = new Set(["mp4", "webm", "mkv", "mov", "m4v", "flv", "mp3", "m4a", "ogg", "opus", "flac", "wav", "aac"]);
const SKIP_HOSTS = ["googlevideo.com", "doubleclick.net", "googlesyndication.com", "fbcdn.net"];

async function loadSettings() {
  settings = await getSettings();
}

browser.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "local") return;
  await loadSettings();
  if (changes.sites || changes.allSites || changes.overlay) {
    const tabs = await browser.tabs.query({});
    for (const t of tabs) {
      if (!t.url) continue;
      const enabled = siteEnabled(settings, t.url);
      if (!enabled) clearTab(t.id);
      browser.tabs.sendMessage(t.id, { type: "site-changed", enabled, overlay: settings.overlay }).catch(() => {});
    }
  }
});

async function bridge(path, body, timeoutMs = 4000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`http://127.0.0.1:${settings.port}${path}`, {
      method: body ? "POST" : "GET",
      headers: { "X-DDM-Client": "firefox", "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      credentials: "omit",
      cache: "no-store"
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

async function ping(force = false) {
  if (!force && appInfo && Date.now() - appCheckedAt < 15000) return appInfo;
  try {
    appInfo = await bridge("/ping", null, 1200);
  } catch (e) {
    appInfo = null;
  }
  appCheckedAt = Date.now();
  return appInfo;
}

async function cookiesFor(url, storeId) {
  const query = { url, firstPartyDomain: null };
  if (storeId) query.storeId = storeId;
  try {
    return await browser.cookies.getAll(query);
  } catch (e) {
    try {
      return await browser.cookies.getAll({ url });
    } catch (e2) {
      return [];
    }
  }
}

function cookieHeader(list) {
  return list.map(c => `${c.name}=${c.value}`).join("; ");
}

function slimCookies(list) {
  return list.map(c => ({
    name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure,
    httpOnly: c.httpOnly, hostOnly: c.hostOnly, expirationDate: c.expirationDate
  }));
}

async function sendToApp({ url, kind = "file", filename, referer, pageUrl, title, size, storeId }, timeoutMs = 6000) {
  const payload = { url, kind, filename, referer, pageUrl, title, size, userAgent: navigator.userAgent };
  if (kind === "file") {
    const list = await cookiesFor(url, storeId);
    if (list.length) payload.cookieHeader = cookieHeader(list);
  } else {
    const list = await cookiesFor(pageUrl || url, storeId);
    if (list.length) payload.cookies = slimCookies(list);
  }
  return bridge("/download", payload, timeoutMs);
}

function classify(url, contentType) {
  const ct = (contentType || "").toLowerCase().split(";")[0].trim();
  const ext = extOf(url);
  if (ext === "m3u8" || ct === "application/vnd.apple.mpegurl" || ct === "application/x-mpegurl" || ct === "audio/mpegurl") {
    return { kind: "video", label: "HLS" };
  }
  if (ext === "mpd" || ct === "application/dash+xml") {
    return { kind: "video", label: "DASH" };
  }
  if (ext === "ts" || ext === "m4s" || ct === "video/mp2t" || ct === "video/iso.segment") return null;
  if (ct.startsWith("video/") || ct.startsWith("audio/")) {
    return { kind: "file", label: (ext || ct.split("/")[1] || "media").toUpperCase() };
  }
  if ((ct === "application/octet-stream" || ct === "binary/octet-stream" || !ct) && MEDIA_EXTS.has(ext)) {
    return { kind: "file", label: ext.toUpperCase() };
  }
  return null;
}

function headerValue(headers, name) {
  const h = (headers || []).find(x => x.name.toLowerCase() === name);
  return h ? h.value : null;
}

function addMedia(tabId, item) {
  if (tabId < 0) return;
  let entry = tabMedia.get(tabId);
  if (!entry) {
    entry = new Map();
    tabMedia.set(tabId, entry);
  }
  const key = item.url.split("#")[0];
  const prev = entry.get(key);
  if (prev) {
    if (!prev.size && item.size) prev.size = item.size;
    return;
  }
  if (entry.size >= 60) return;
  entry.set(key, Object.assign({ found: Date.now() }, item));
  updateBadge(tabId);
}

function clearTab(tabId) {
  tabMedia.delete(tabId);
  updateBadge(tabId);
}

function updateBadge(tabId) {
  const n = tabMedia.has(tabId) ? tabMedia.get(tabId).size : 0;
  browser.browserAction.setBadgeText({ tabId, text: n ? String(n) : "" }).catch(() => {});
  browser.browserAction.setBadgeBackgroundColor({ tabId, color: "#3584e4" }).catch(() => {});
}

browser.webRequest.onHeadersReceived.addListener(details => {
  if (details.tabId < 0) return;
  const pageUrl = tabUrls.get(details.tabId) || details.documentUrl || details.originUrl;
  if (!pageUrl || !siteEnabled(settings, pageUrl)) return;
  if (details.statusCode >= 400) return;
  const host = hostOf(details.url);
  if (SKIP_HOSTS.some(h => host === h || host.endsWith("." + h))) return;
  const ct = headerValue(details.responseHeaders, "content-type");
  const info = classify(details.url, ct);
  if (!info) return;
  let size = null;
  const range = headerValue(details.responseHeaders, "content-range");
  const m = range && range.match(/\/(\d+)\s*$/);
  if (m) size = parseInt(m[1], 10);
  else {
    const cl = headerValue(details.responseHeaders, "content-length");
    if (cl && /^\d+$/.test(cl)) size = parseInt(cl, 10);
  }
  if (info.kind === "file" && size !== null && size < 256 * 1024) return;
  addMedia(details.tabId, {
    url: details.url, kind: info.kind, label: info.label, size,
    name: baseName(details.url.split(/[?#]/)[0]) || info.label, pageUrl
  });
}, { urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest", "object", "other"] }, ["responseHeaders"]);

browser.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.url) {
    const prev = tabUrls.get(tabId);
    tabUrls.set(tabId, change.url);
    if (prev && prev.split("#")[0] !== change.url.split("#")[0]) clearTab(tabId);
  } else if (tab && tab.url && !tabUrls.has(tabId)) {
    tabUrls.set(tabId, tab.url);
  }
  if (change.status === "loading" && change.url) clearTab(tabId);
});

browser.tabs.onRemoved.addListener(tabId => {
  tabMedia.delete(tabId);
  tabUrls.delete(tabId);
});

const RENDERABLE = /^(text\/|image\/|video\/|audio\/|application\/(xhtml\+xml|xml|json|javascript|pdf|x-javascript|ecmascript|rss\+xml|atom\+xml|ld\+json))/;
const DOWNLOAD_TYPES = /^application\/(octet-stream|zip|x-zip|x-zip-compressed|gzip|x-gzip|x-tar|x-gtar|x-xz|x-bzip2?|zstd|x-7z-compressed|vnd\.rar|x-rar|x-rar-compressed|x-iso9660-image|x-debian-package|vnd\.debian\.binary-package|x-rpm|x-redhat-package-manager|x-msdownload|x-msdos-program|x-msi|x-apple-diskimage|vnd\.android\.package-archive|x-executable|x-sh|x-appimage|epub\+zip|binary|force-download|download)/;

function filenameFromDisposition(cd) {
  if (!cd) return "";
  let m = cd.match(/filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i);
  if (m) {
    try {
      return decodeURIComponent(m[2].trim().replace(/^"|"$/g, ""));
    } catch (e) {
      return m[2].trim();
    }
  }
  m = cd.match(/filename\s*=\s*"([^"]*)"/i) || cd.match(/filename\s*=\s*([^;]+)/i);
  if (!m) return "";
  const raw = m[1].trim();
  if ([...raw].every(c => c.charCodeAt(0) < 256)) {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(raw, c => c.charCodeAt(0)));
    } catch (e) {
      return raw;
    }
  }
  return raw;
}

function freshApp() {
  return appInfo && Date.now() - appCheckedAt < 30000 ? appInfo : null;
}

async function closeIfBlank(tabId) {
  if (tabId < 0) return;
  try {
    const tab = await browser.tabs.get(tabId);
    if (!tab.url || tab.url === "about:blank" || tab.url === "about:newtab") {
      const tabs = await browser.tabs.query({ windowId: tab.windowId });
      if (tabs.length > 1) await browser.tabs.remove(tabId);
    }
  } catch (e) {
  }
}

browser.webRequest.onHeadersReceived.addListener(async details => {
  if (!settings.capture || details.method !== "GET") return {};
  if (details.statusCode < 200 || details.statusCode >= 300) return {};
  const info = freshApp();
  if (!info) return {};
  const cd = headerValue(details.responseHeaders, "content-disposition") || "";
  const ct = (headerValue(details.responseHeaders, "content-type") || "").split(";")[0].trim().toLowerCase();
  const attachment = /^\s*attachment/i.test(cd);
  if (!attachment && (RENDERABLE.test(ct) || (ct && !DOWNLOAD_TYPES.test(ct)))) return {};
  let name = filenameFromDisposition(cd);
  if (!name) {
    try {
      name = decodeURIComponent(baseName(new URL(details.url).pathname));
    } catch (e) {
      name = baseName(details.url.split(/[?#]/)[0]);
    }
  }
  const exts = new Set((info.config && info.config.capture_exts) || []);
  if (!exts.has(extOf(name))) return {};
  const cl = headerValue(details.responseHeaders, "content-length");
  const size = cl && /^\d+$/.test(cl) ? parseInt(cl, 10) : undefined;
  const minBytes = ((info.config && info.config.capture_min_kib) || 0) * 1024;
  if (minBytes && size !== undefined && size < minBytes) return {};
  try {
    await sendToApp({
      url: details.url, kind: "file", filename: name, size,
      referer: details.originUrl || details.documentUrl || undefined,
      storeId: details.cookieStoreId
    }, 2500);
  } catch (e) {
    return {};
  }
  handedOff.add(details.url);
  setTimeout(() => handedOff.delete(details.url), 15000);
  if (details.type === "main_frame") setTimeout(() => closeIfBlank(details.tabId), 300);
  return { cancel: true };
}, { urls: ["http://*/*", "https://*/*"], types: ["main_frame", "sub_frame"] }, ["blocking", "responseHeaders"]);

browser.downloads.onCreated.addListener(async item => {
  if (!settings.capture) return;
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url)) return;
  if (handedOff.has(url)) {
    handedOff.delete(url);
    browser.downloads.cancel(item.id).then(() => browser.downloads.erase({ id: item.id })).catch(() => {});
    return;
  }
  const info = freshApp() || await ping();
  if (!info) return;
  const exts = new Set((info.config && info.config.capture_exts) || []);
  const name = baseName(item.filename) || baseName(url.split(/[?#]/)[0]);
  const ext = extOf(name) || extOf(url);
  if (!exts.has(ext)) return;
  const minBytes = ((info.config && info.config.capture_min_kib) || 0) * 1024;
  if (minBytes && item.totalBytes > 0 && item.totalBytes < minBytes) return;
  try {
    await browser.downloads.cancel(item.id);
  } catch (e) {
    return;
  }
  browser.downloads.erase({ id: item.id }).catch(() => {});
  try {
    await sendToApp({
      url, kind: "file", filename: name, referer: item.referrer || undefined,
      size: item.totalBytes > 0 ? item.totalBytes : undefined, storeId: item.cookieStoreId
    });
  } catch (e) {
    handedOff.add(url);
    browser.downloads.download({ url, filename: name || undefined }).catch(() => handedOff.delete(url));
  }
});

function setupMenus() {
  browser.contextMenus.removeAll().then(() => {
    browser.contextMenus.create({ id: "yd-link", title: t("menuLink"), contexts: ["link"] });
    browser.contextMenus.create({ id: "yd-media", title: t("menuMedia"), contexts: ["video", "audio"] });
    browser.contextMenus.create({ id: "yd-page", title: t("menuPage"), contexts: ["page", "frame"] });
  });
}

async function downloadMediaElement(src, frameUrl, tab) {
  const pageUrl = tab ? tab.url : frameUrl;
  if (src && /^https?:/i.test(src)) {
    const info = classify(src, "");
    const kind = info && info.kind === "video" ? "video" : "file";
    return sendToApp({
      url: src, kind, referer: frameUrl || pageUrl, pageUrl: frameUrl || pageUrl,
      title: tab && tab.title, storeId: tab && tab.cookieStoreId
    });
  }
  const target = frameUrl && frameUrl !== "about:blank" ? frameUrl : pageUrl;
  return sendToApp({ url: target, kind: "video", pageUrl: target, title: tab && tab.title, storeId: tab && tab.cookieStoreId });
}

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "yd-link") {
      await sendToApp({
        url: info.linkUrl, kind: isVideoSite(info.linkUrl) ? "video" : "file",
        referer: info.pageUrl, pageUrl: isVideoSite(info.linkUrl) ? info.linkUrl : info.pageUrl,
        storeId: tab && tab.cookieStoreId
      });
    } else if (info.menuItemId === "yd-media") {
      await downloadMediaElement(info.srcUrl, info.frameUrl || info.pageUrl, tab);
    } else if (info.menuItemId === "yd-page") {
      const target = info.frameUrl || info.pageUrl;
      await sendToApp({ url: target, kind: "video", pageUrl: target, title: tab && tab.title, storeId: tab && tab.cookieStoreId });
    }
  } catch (e) {
    notifyFailure(tab, e);
  }
});

function notifyFailure(tab, err) {
  if (tab && tab.id >= 0) {
    browser.browserAction.setBadgeText({ tabId: tab.id, text: "!" }).catch(() => {});
    browser.browserAction.setBadgeBackgroundColor({ tabId: tab.id, color: "#e01b24" }).catch(() => {});
    setTimeout(() => updateBadge(tab.id), 4000);
  }
  console.warn("DDM:", err);
}

browser.runtime.onMessage.addListener((msg, sender) => {
  const tab = sender.tab;
  switch (msg.type) {
    case "site-enabled?": {
      const top = tab ? (tabUrls.get(tab.id) || tab.url) : "";
      return Promise.resolve({ enabled: siteEnabled(settings, top), overlay: settings.overlay });
    }
    case "media-found": {
      if (!tab) return;
      const top = tabUrls.get(tab.id) || tab.url;
      if (!siteEnabled(settings, top)) return;
      for (const it of msg.items || []) {
        const info = classify(it.url, "");
        addMedia(tab.id, {
          url: it.url, kind: info && info.kind === "video" ? "video" : "file",
          label: info ? info.label : (extOf(it.url) || "video").toUpperCase(),
          size: null, name: baseName(it.url.split(/[?#]/)[0]) || "video", pageUrl: msg.frameUrl || top
        });
      }
      return;
    }
    case "overlay-download":
      return downloadMediaElement(msg.src, msg.frameUrl, tab).then(r => ({ ok: true, r }), e => ({ ok: false, error: String(e.message || e) }));
    case "popup-state":
      return popupState(msg.tabId);
    case "popup-download":
      return browser.tabs.get(msg.tabId).then(t => sendToApp(Object.assign({ storeId: t.cookieStoreId, title: t.title }, msg.payload)))
        .then(r => ({ ok: true, r }), e => ({ ok: false, error: String(e.message || e) }));
    case "popup-downloads":
      return bridge("/downloads", null, 1500).then(r => r.downloads, () => null);
    case "popup-action":
      return bridge("/action", { id: msg.id, action: msg.action }, 2000).then(() => ({ ok: true }), e => ({ ok: false, error: String(e) }));
    case "ping":
      return ping(true);
  }
});

async function popupState(tabId) {
  const tab = await browser.tabs.get(tabId);
  const url = tabUrls.get(tabId) || tab.url;
  const items = tabMedia.has(tabId) ? Array.from(tabMedia.get(tabId).values()) : [];
  items.sort((a, b) => (b.kind === "video") - (a.kind === "video") || (b.size || 0) - (a.size || 0));
  return {
    url, title: tab.title, host: siteKey(hostOf(url)),
    enabled: siteEnabled(settings, url), allSites: settings.allSites,
    capture: settings.capture, videoSite: isVideoSite(url),
    items, app: await ping(true)
  };
}

(async () => {
  await loadSettings();
  setupMenus();
  const tabs = await browser.tabs.query({});
  for (const t of tabs) if (t.url) tabUrls.set(t.id, t.url);
  ping(true);
  setInterval(() => ping(true), 10000);
})();
