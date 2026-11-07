// Runs in every document of a Designer tab (bridge.py relay_attach fills __CONFIG__). See README "Relay".
// Designer page: holds the socket to relay.py, reconnects by itself, and forwards calls to the bridge app frame.
// It has to live here because Chrome blocks local network access from the cross-origin extension frame.
// Bridge app frame: runs each call against the Designer API and posts the result back.
(() => {
  const CFG = __CONFIG__;
  if (window.__relayOn) return;
  const host = location.hostname;
  const isDesigner = window === window.top && host.endsWith('.design.webflow.com');
  const isBridge = window.parent === window.top && host.endsWith('.webflow-ext.com');
  if (!isDesigner && !isBridge) return;
  window.__relayOn = true;

  if (isBridge) {
    const AsyncFn = (async () => {}).constructor;
    addEventListener('message', async e => {
      if (e.source !== window.parent || !e.origin.endsWith('.design.webflow.com') || !e.data || !e.data.__relayQ) return;
      const m = JSON.parse(e.data.__relayQ), t0 = performance.now();
      let out;
      try { out = { id: m.id, ok: true, v: await new AsyncFn('a', CFG.prelude + m.code)(m.arg) } }
      catch (x) { out = { id: m.id, ok: false, err: String(x && x.message || x).slice(0, 1000) } }
      out.ms = Math.round(performance.now() - t0);
      let text;
      try { text = JSON.stringify(out) }
      catch (x) { text = JSON.stringify({ id: m.id, ok: false, err: 'RESULT NOT SERIALIZABLE: ' + x.message, ms: out.ms }) }
      e.source.postMessage({ __relayR: text }, e.origin);
    });
    return;
  }

  let ws = null, wait = 1000;
  const bridgeFrame = () => [...document.querySelectorAll('iframe')].reverse().find(f => (f.src || '').includes('.webflow-ext.com/'));
  const connect = () => {
    const s = new WebSocket(`ws://127.0.0.1:${CFG.port}/?t=${CFG.token}`);
    ws = s;
    s.onopen = () => { wait = 1000 };
    s.onmessage = ev => {
      const f = bridgeFrame();
      if (!f) { const m = JSON.parse(ev.data); s.send(JSON.stringify({ id: m.id, ok: false, err: 'BRIDGE APP NOT OPEN', ms: 0 })); return }
      f.contentWindow.postMessage({ __relayQ: ev.data }, new URL(f.src).origin);
    };
    s.onclose = () => { setTimeout(connect, wait); wait = Math.min(wait * 2, 3000) };
  };
  addEventListener('message', e => {
    if (e.origin.endsWith('.webflow-ext.com') && e.data && e.data.__relayR && ws && ws.readyState === 1) ws.send(e.data.__relayR);
  });
  connect();
})();
