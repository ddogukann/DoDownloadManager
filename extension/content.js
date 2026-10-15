(() => {
  if (window.__ddmLoaded) return;
  window.__ddmLoaded = true;

  let enabled = false;
  let overlayOn = true;
  let observer = null;
  let scanTimer = null;
  const reported = new Set();
  let host = null;
  let button = null;
  let current = null;
  let rafPending = false;
  let hideTimer = null;

  function mediaSources(el) {
    const out = [];
    const add = u => {
      if (u && /^https?:/i.test(u)) out.push(u);
    };
    add(el.currentSrc);
    add(el.getAttribute("src") ? el.src : "");
    el.querySelectorAll("source[src]").forEach(s => add(s.src));
    return out;
  }

  function scan() {
    scanTimer = null;
    if (!enabled) return;
    const items = [];
    document.querySelectorAll("video, audio").forEach(el => {
      for (const u of mediaSources(el)) {
        if (!reported.has(u)) {
          reported.add(u);
          items.push({ url: u });
        }
      }
    });
    if (items.length) {
      browser.runtime.sendMessage({ type: "media-found", items, frameUrl: location.href }).catch(() => {});
    }
  }

  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(scan, 800);
  }

  function onMediaEvent(e) {
    if (e.target && (e.target.tagName === "VIDEO" || e.target.tagName === "AUDIO")) scheduleScan();
  }

  function buildButton() {
    host = document.createElement("div");
    host.style.cssText = "all: initial; position: fixed; z-index: 2147483647; top: 0; left: 0; display: none;";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `
      <style>
        button {
          all: unset; box-sizing: border-box; display: flex; align-items: center; gap: 6px;
          padding: 6px 11px 6px 9px; border-radius: 999px; cursor: pointer;
          font: 600 12.5px/1 system-ui, "Cantarell", "Ubuntu", sans-serif; color: #fff;
          background: rgba(20, 20, 24, 0.82); backdrop-filter: blur(8px);
          box-shadow: 0 2px 10px rgba(0,0,0,.35), inset 0 0 0 1px rgba(255,255,255,.12);
          transition: background .15s ease, transform .15s ease;
        }
        button:hover { background: #3584e4; }
        button:active { transform: scale(.96); }
        button.ok { background: #26a269; }
        button.err { background: #c01c28; }
        svg { width: 15px; height: 15px; flex: none; }
      </style>
      <button type="button" title="DDM ile indir">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M11 3h2v10.2l3.6-3.6 1.4 1.4-6 6-6-6 1.4-1.4 3.6 3.6zM5 19h14v2H5z"/></svg>
        <span>İndir</span>
      </button>`;
    button = root.querySelector("button");
    const label = root.querySelector("span");
    button.addEventListener("click", async e => {
      e.preventDefault();
      e.stopPropagation();
      if (!current) return;
      const src = mediaSources(current)[0] || "";
      label.textContent = "Gönderiliyor…";
      let res;
      try {
        res = await browser.runtime.sendMessage({ type: "overlay-download", src, frameUrl: location.href });
      } catch (err) {
        res = { ok: false };
      }
      button.classList.add(res && res.ok ? "ok" : "err");
      label.textContent = res && res.ok ? "DDM'ye gönderildi" : "Uygulama çalışmıyor";
      setTimeout(() => {
        button.classList.remove("ok", "err");
        label.textContent = "İndir";
      }, 2200);
    }, true);
    host.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    (document.body || document.documentElement).appendChild(host);
  }

  function place(video) {
    const r = video.getBoundingClientRect();
    if (r.width < 200 || r.height < 110 || r.bottom < 0 || r.top > innerHeight) {
      hide();
      return;
    }
    if (!host) buildButton();
    if (!host.isConnected) (document.body || document.documentElement).appendChild(host);
    current = video;
    host.style.display = "block";
    host.style.top = `${Math.max(4, r.top + 10)}px`;
    host.style.left = `${Math.min(innerWidth - 150, r.right - 140)}px`;
  }

  function hide() {
    if (host) host.style.display = "none";
    current = null;
  }

  function findVideoAt(x, y) {
    for (const el of document.elementsFromPoint(x, y)) {
      if (el.tagName === "VIDEO") return el;
    }
    let best = null;
    document.querySelectorAll("video").forEach(v => {
      const r = v.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom && r.width >= 200) best = v;
    });
    return best;
  }

  function onMouseMove(e) {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      if (!enabled || !overlayOn) return;
      if (host && e.composedPath && e.composedPath().includes(host)) return;
      const v = findVideoAt(e.clientX, e.clientY);
      if (v) {
        clearTimeout(hideTimer);
        place(v);
      } else if (current) {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(hide, 600);
      }
    });
  }

  function onScroll() {
    if (current) place(current);
  }

  function start() {
    if (observer) return;
    document.addEventListener("loadedmetadata", onMediaEvent, true);
    document.addEventListener("play", onMediaEvent, true);
    observer = new MutationObserver(scheduleScan);
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
    if (overlayOn) {
      document.addEventListener("mousemove", onMouseMove, { passive: true, capture: true });
      addEventListener("scroll", onScroll, { passive: true, capture: true });
    }
    scheduleScan();
  }

  function stop() {
    if (observer) {
      observer.disconnect();
      observer = null;
    }
    document.removeEventListener("loadedmetadata", onMediaEvent, true);
    document.removeEventListener("play", onMediaEvent, true);
    document.removeEventListener("mousemove", onMouseMove, { capture: true });
    removeEventListener("scroll", onScroll, { capture: true });
    hide();
  }

  function apply(state) {
    const wasOverlay = overlayOn;
    enabled = !!state.enabled;
    overlayOn = state.overlay !== false;
    if (!enabled) {
      stop();
      return;
    }
    if (observer && wasOverlay !== overlayOn) stop();
    start();
  }

  browser.runtime.onMessage.addListener(msg => {
    if (msg && msg.type === "site-changed") apply(msg);
  });

  browser.runtime.sendMessage({ type: "site-enabled?" }).then(apply, () => {});
})();
