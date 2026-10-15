let tabId = null;
let state = null;
let pollTimer = null;

const $ = id => document.getElementById(id);

function safeDecode(s) {
  try {
    return decodeURIComponent(s || "");
  } catch (e) {
    return s || "";
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function setStatus(app) {
  const s = $("app-status");
  s.classList.toggle("ok", !!app);
  s.classList.toggle("bad", !app);
  s.querySelector("span").textContent = app
    ? `Bağlı · v${app.version}${app.active ? ` · ${app.active} etkin` : ""}`
    : "Uygulama çalışmıyor";
  $("offline").hidden = !!app;
}

async function send(payload, btn) {
  const original = btn ? Array.from(btn.childNodes, n => n.cloneNode(true)) : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Gönderiliyor…";
  }
  const res = await browser.runtime.sendMessage({ type: "popup-download", tabId, payload });
  if (btn) {
    btn.textContent = res && res.ok ? "Gönderildi ✓" : "Hata";
    btn.classList.toggle("done", !!(res && res.ok));
    setTimeout(() => {
      btn.replaceChildren(...original);
      btn.disabled = false;
      btn.classList.remove("done");
    }, 1800);
  }
  refreshDownloads();
}

function renderMedia() {
  const list = $("media");
  list.textContent = "";
  const items = state.items || [];
  $("media-section").hidden = !state.enabled || !items.length;
  $("media-count").textContent = items.length ? String(items.length) : "";
  $("empty-hint").hidden = !state.enabled || items.length > 0 || state.videoSite;
  $("scan-hint").hidden = state.enabled;
  for (const it of items) {
    const li = el("li", "item");
    const badge = el("span", `badge ${it.kind === "video" ? "stream" : ""}`, it.label);
    const info = el("div", "info");
    const name = el("span", "name", safeDecode(it.name).slice(0, 120) || it.label);
    name.title = it.url;
    const meta = el("span", "meta", [it.size ? humanSize(it.size) : "", hostOf(it.url)].filter(Boolean).join(" · "));
    info.append(name, meta);
    const btn = el("button", "small");
    btn.textContent = "İndir";
    btn.addEventListener("click", () => send({
      url: it.url, kind: it.kind, referer: it.pageUrl || state.url, pageUrl: it.pageUrl || state.url
    }, btn));
    li.append(badge, info, btn);
    list.append(li);
  }
}

function render() {
  setStatus(state.app);
  $("host").textContent = state.host || "bu sayfa";
  const toggle = $("site-toggle");
  toggle.checked = state.enabled;
  toggle.disabled = !state.host || state.allSites;
  if (state.allSites) $("host").textContent = `${state.host} · tüm sitelerde açık`;
  $("capture-toggle").checked = state.capture;
  $("page-video").hidden = !(state.host && (state.videoSite || (state.enabled && (state.items || []).some(i => i.kind === "video"))));
  renderMedia();
}

const STATUS = { queued: "Sırada", running: "İndiriliyor", paused: "Duraklatıldı", done: "Tamamlandı", error: "Hata" };

async function refreshDownloads() {
  const list = await browser.runtime.sendMessage({ type: "popup-downloads" });
  const ul = $("downloads");
  ul.textContent = "";
  if (!list || !list.length) {
    $("dl-section").hidden = true;
    return;
  }
  $("dl-section").hidden = false;
  for (const d of list.slice(0, 5)) {
    const li = el("li", "item dl");
    const info = el("div", "info");
    info.append(el("span", "name", d.filename || d.url));
    const pct = d.total ? Math.min(100, Math.round((d.downloaded / d.total) * 100)) : d.status === "done" ? 100 : 0;
    const bar = el("div", `bar ${d.status}`);
    const fill = el("i");
    fill.style.width = `${pct}%`;
    bar.append(fill);
    info.append(bar);
    const bits = [STATUS[d.status] || d.status];
    if (d.status === "running" && d.speed) bits.push(`${humanSize(Math.round(d.speed))}/s`);
    if (d.total) bits.push(d.status === "done" ? humanSize(d.total) : `%${pct}`);
    if (d.status === "error" && d.error) bits.push(d.error);
    const vt = d.vt || {};
    if (vt.mismatch) bits.push("≠ hash uyuşmuyor");
    else if (vt.verdict === "clean") bits.push(`VT ✓ 0/${vt.total}`);
    else if (vt.verdict === "suspicious") bits.push(`VT ⚠ ${vt.malicious}/${vt.total}`);
    else if (vt.verdict === "malicious") bits.push(`VT ✕ ${vt.malicious}/${vt.total}`);
    info.append(el("span", "meta", bits.join(" · ")));
    li.append(info);
    if (d.status === "running" || d.status === "queued" || d.status === "paused" || d.status === "error") {
      const action = d.status === "running" || d.status === "queued" ? "pause" : "resume";
      const b = el("button", "icon small", action === "pause" ? "❚❚" : "▶");
      b.title = action === "pause" ? "Duraklat" : "Devam et";
      b.addEventListener("click", async () => {
        await browser.runtime.sendMessage({ type: "popup-action", id: d.id, action });
        refreshDownloads();
      });
      li.append(b);
    }
    ul.append(li);
  }
}

async function load() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  tabId = tab.id;
  state = await browser.runtime.sendMessage({ type: "popup-state", tabId });
  render();
  refreshDownloads();
}

$("site-toggle").addEventListener("change", async e => {
  const settings = await getSettings();
  const sites = Object.assign({}, settings.sites);
  if (e.target.checked) sites[state.host] = true;
  else {
    for (const k of Object.keys(sites)) {
      if (state.host === k || state.host.endsWith("." + k)) delete sites[k];
    }
  }
  await browser.storage.local.set({ sites });
  setTimeout(load, 150);
});

$("capture-toggle").addEventListener("change", e => browser.storage.local.set({ capture: e.target.checked }));
$("open-options").addEventListener("click", () => browser.runtime.openOptionsPage());
$("download-page").addEventListener("click", e => send({ url: state.url, kind: "video", pageUrl: state.url }, e.currentTarget));

load();
pollTimer = setInterval(() => {
  refreshDownloads();
  browser.runtime.sendMessage({ type: "popup-state", tabId }).then(s => {
    const changed = !state || JSON.stringify(s.items) !== JSON.stringify(state.items) || !!s.app !== !!state.app;
    state = s;
    if (changed) render();
    else setStatus(s.app);
  });
}, 1500);
