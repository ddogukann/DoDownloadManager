const UI_TR = (browser.i18n.getUILanguage() || "").toLowerCase().startsWith("tr");

function t(key, subs) {
  return browser.i18n.getMessage(key, subs) || key;
}

function pctText(n) {
  return UI_TR ? `%${n}` : `${n}%`;
}

function applyI18n(root = document) {
  document.documentElement.lang = UI_TR ? "tr" : "en";
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
}

const DEFAULTS = {
  port: 47821,
  capture: true,
  overlay: true,
  allSites: false,
  sites: {}
};

async function getSettings() {
  const stored = await browser.storage.local.get(Object.keys(DEFAULTS));
  return Object.assign({}, DEFAULTS, stored);
}

function hostOf(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.hostname.toLowerCase();
  } catch (e) {
    return "";
  }
}

function siteKey(host) {
  return (host || "").replace(/^www\./, "").replace(/^m\./, "");
}

function siteEnabled(settings, url) {
  const host = hostOf(url);
  if (!host) return false;
  if (settings.allSites) return true;
  const parts = siteKey(host).split(".");
  for (let i = 0; i < parts.length - 1; i++) {
    if (settings.sites[parts.slice(i).join(".")]) return true;
  }
  return false;
}

function extOf(name) {
  const clean = (name || "").split(/[?#]/)[0];
  const base = clean.split("/").pop() || "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

function baseName(path) {
  return (path || "").split(/[\\/]/).pop();
}

function humanSize(n) {
  if (n === null || n === undefined || n < 0 || isNaN(n)) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let f = n;
  for (const u of units) {
    if (f < 1024 || u === "TB") return u === "B" ? `${f} ${u}` : `${UI_TR ? f.toFixed(1).replace(".", ",") : f.toFixed(1)} ${u}`;
    f /= 1024;
  }
  return String(n);
}

const VIDEO_SITES = [
  "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv", "x.com", "twitter.com",
  "instagram.com", "tiktok.com", "facebook.com", "reddit.com", "soundcloud.com", "bilibili.com",
  "streamable.com", "kick.com", "rumble.com", "bandcamp.com"
];

function isVideoSite(url) {
  const host = siteKey(hostOf(url));
  return VIDEO_SITES.some(s => host === s || host.endsWith("." + s));
}
