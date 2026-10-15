const $ = id => document.getElementById(id);

async function render() {
  const s = await getSettings();
  $("port").value = s.port;
  $("capture").checked = s.capture;
  $("overlay").checked = s.overlay;
  $("allSites").checked = s.allSites;
  const ul = $("sites");
  ul.textContent = "";
  const keys = Object.keys(s.sites).filter(k => s.sites[k]).sort();
  $("no-sites").hidden = keys.length > 0;
  ul.hidden = keys.length === 0;
  for (const k of keys) {
    const li = document.createElement("li");
    li.className = "item";
    const name = document.createElement("span");
    name.className = "name info";
    name.textContent = k;
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = t("remove");
    btn.addEventListener("click", async () => {
      const cur = await getSettings();
      const sites = Object.assign({}, cur.sites);
      delete sites[k];
      await browser.storage.local.set({ sites });
      render();
    });
    li.append(name, btn);
    ul.append(li);
  }
}

async function test() {
  $("status").textContent = t("testing");
  const info = await browser.runtime.sendMessage({ type: "ping" });
  $("status").textContent = info
    ? `${t("connectedTo", [info.app, info.version])} · ${info.ytdlp ? t("ytdlpReady") : t("ytdlpMissing")}`
    : t("cannotReachApp");
}

$("port").addEventListener("change", async e => {
  const p = parseInt(e.target.value, 10);
  if (p >= 1024 && p <= 65535) {
    await browser.storage.local.set({ port: p });
    test();
  }
});
$("capture").addEventListener("change", e => browser.storage.local.set({ capture: e.target.checked }));
$("overlay").addEventListener("change", e => browser.storage.local.set({ overlay: e.target.checked }));
$("allSites").addEventListener("change", e => browser.storage.local.set({ allSites: e.target.checked }));
$("test").addEventListener("click", test);

applyI18n();
render();
test();
