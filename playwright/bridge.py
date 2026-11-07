"""Webflow Designer helpers. driver.py loads this file into every snippet's globals (and reloads it when it changes),
so snippets call these directly. Uses the driver globals context, evaluate_t, check and progress.

    open_site("ca-bps")  # or a Designer URL / sites/<key>.json key: opens or reuses its tab, launches the bridge
    r = js("const h = await byId(a); return await h.getDisplayName()", element_id)
    r = js("await tm('rename', () => h.setDisplayName('x')); return 1")   # r["T"] / r["E"]: per-call ms / errors
    switch_page("Home"); open_canvas(component_id); designer(); canvas(); live("/")
    t = live("/"); emulate(t, 390); t.reload(); snap(t, "C:/tmp/m.png", full=True); clear_emulation(t)
    until("return (await webflow.getSelectedElement())?.type === 'Heading'")
    paste_at(1480, 751, "new text")

Sites: nothing here is tied to one Webflow project. Each site has local settings in sites/<key>.json (created on first
open_site). The site a snippet works on comes from `x.py --site <key>` (or use()/open_site() inside the snippet) and
lasts for that snippet only. Without one, the helpers work only while exactly one Designer tab is open; with several
they refuse rather than guess, so a call never lands in another project's tab.

Every js() call resolves the bridge frame again, so it survives page and canvas switches."""
import json as _json, os, re, time
from urllib.parse import urlparse

# Available inside every js() body: `a` (the arg), tm(label, fn, ms=8000) to time one call and keep going on error
# or hang (some invalid bridge calls never settle), byId(elementId) to find an element on the current page or
# canvas (one getAllElements call). T/E also live on window, so js() can recover them after a whole-call timeout.
_PRELUDE = """
const T = (window.__tmT = {}), E = (window.__tmE = {});
const tm = async (k, f, ms = 8000) => { const t = performance.now(); let h;
  try { const v = await Promise.race([f(), new Promise((_, r) => { h = setTimeout(() => r(new Error('CALL TIMEOUT ' + ms + 'ms')), ms) })]);
    T[k] = Math.round(performance.now() - t); return v }
  catch (e) { T[k] = Math.round(performance.now() - t); E[k] = String(e && e.message || e).slice(0, 300) }
  finally { clearTimeout(h) } };
const byId = async id => (await webflow.getAllElements()).find(e => e.id.element === (id.element || id));
"""


DESIGN, STAGING, EXT = ".design.webflow.com", ".webflow.io", "webflow-ext.com"
# The driver provides HERE, SITE (per snippet) and block_media; fall back so an older driver keeps working.
SITE = globals().get("SITE")
SITES = os.path.join(globals().get("HERE") or os.getcwd(), "sites")
if "block_media" not in globals():
    def block_media(designer_tab=None):
        pass


def _host(url):
    return (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()


def short_name(ref):
    """'ca-bps' from 'https://ca-bps.design.webflow.com/...', 'ca-bps.webflow.io' or similar; else None."""
    h = _host(ref.strip())
    for suffix in (DESIGN, STAGING):
        if h.endswith(suffix) and h[:-len(suffix)] and "." not in h[:-len(suffix)]:
            return h[:-len(suffix)]
    return None


def sites():
    """All local site settings: {key: settings} from sites/*.json (example.json is a template, not a site)."""
    os.makedirs(SITES, exist_ok=True)
    out = {}
    for f in sorted(os.listdir(SITES)):
        if f.endswith(".json") and f != "example.json":
            out[f[:-5]] = dict(_json.load(open(os.path.join(SITES, f), encoding="utf-8")), key=f[:-5])
    return out


def save_site(key, **fields):
    """Create or update sites/<key>.json (None values are skipped). Fills designer/staging URLs from shortName."""
    os.makedirs(SITES, exist_ok=True)
    path = os.path.join(SITES, key + ".json")
    cfg = _json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    cfg.update({k: v for k, v in fields.items() if v is not None})
    cfg.setdefault("shortName", key)
    cfg.setdefault("designer", f"https://{cfg['shortName']}{DESIGN}/")
    cfg.setdefault("staging", f"https://{cfg['shortName']}{STAGING}/")
    _json.dump(cfg, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return dict(cfg, key=key)


def site(ref=None, create=False):
    """Settings of a site by key, short name, site id, name or any of its URLs; defaults to this snippet's SITE.
    A Designer/staging URL of an unknown site always gets a new sites/<short>.json (so `x.py --site <URL>` works
    for a brand-new project); a bare short name only with create=True."""
    ref = ref or SITE
    if not ref:
        return None
    from_url = short_name(ref)
    sn = from_url or ref.strip().lower()
    for key, cfg in sites().items():
        if ref in (key, cfg.get("siteId"), cfg.get("name")) or sn == cfg.get("shortName"):
            return cfg
    if (create or from_url) and re.fullmatch(r"[a-z0-9-]+", sn):
        return save_site(sn, shortName=sn)
    raise RuntimeError(f"UNKNOWN SITE {ref!r}: open_site('<Designer URL>') creates its settings, or add sites/<key>.json")


def use(ref):
    """Work on this site for the rest of the snippet (same as x.py --site). Returns its settings."""
    global SITE
    cfg = site(ref, create=True)
    SITE = cfg["key"]
    return cfg


def designer_tabs():
    return [p for p in context.pages if _host(p.url).endswith(DESIGN)]


def designer(ref=None):
    """This snippet's site's Designer tab. Without a site, the only open Designer tab; with several, an error
    naming them (never a guess: a wrong guess edits another project)."""
    tabs = designer_tabs()
    cfg = site(ref) if (ref or SITE) else None
    if cfg:
        host = cfg["shortName"] + DESIGN
        tab = next((p for p in tabs if _host(p.url) == host), None)
        if not tab:
            raise RuntimeError(f"DESIGNER NOT OPEN for {cfg['key']}: open_site('{cfg['key']}')")
        return tab
    if len(tabs) == 1:
        return tabs[0]
    if not tabs:
        raise RuntimeError("NO DESIGNER OPEN: open_site('<key or Designer URL>')")
    names = ", ".join(_host(p.url)[:-len(DESIGN)] for p in tabs)
    raise RuntimeError(f"SEVERAL DESIGNERS OPEN ({names}): pass x.py --site <key> or call use('<key>')")


def bridge_frame(ref=None):
    """The MCP Bridge App frame in this site's Designer (the newest live one: a reload leaves detached copies), or
    an error saying to launch it."""
    f = next((f for f in reversed(designer(ref).frames) if EXT in f.url and not f.is_detached()), None)
    if not f:
        raise RuntimeError("BRIDGE APP NOT OPEN: launch_bridge() (minimize it, never close it)")
    return f


def canvas(ref=None):
    """The Designer canvas frame (the visible one, not the hidden responsive-images copy)."""
    f = next((f for f in designer(ref).frames if f.name == "site-iframe-next"), None)
    if not f:
        raise RuntimeError("CANVAS NOT FOUND: is the Designer loaded?")
    return f


def _wait_designer(d, timeout):
    t0 = time.time()
    while not (d.get_by_text("Style", exact=True).count() and d.get_by_text("Settings", exact=True).count()):
        check()
        if time.time() - t0 > timeout:
            raise RuntimeError("Designer did not load")
        d.wait_for_timeout(1000)
    d.wait_for_timeout(2000)


def _tab(url):
    """A tab already on url's host, else a blank one or a new one. A Designer tab gets video blocked before it
    navigates; every other tab plays video normally."""
    h = _host(url)
    tab = (next((p for p in context.pages if _host(p.url) == h), None)
           or next((p for p in context.pages if p.url == "about:blank"), None) or context.new_page())
    block_media(tab if h.endswith(DESIGN) else None)
    return tab


def open_site(ref, bridge=True, timeout=120):
    """Open (or reuse) a site's Designer and make it this snippet's site. ref: sites/<key>.json key, short name, or
    Designer URL (an unknown site gets a sites/<short>.json). Launches the bridge, then records the site's id and
    name from webflow.getSiteInfo() and checks the bridge belongs to this site. Returns its settings."""
    cfg = use(ref)
    d = _tab(cfg["designer"])
    if _host(d.url) != _host(cfg["designer"]):
        d.goto(cfg["designer"], wait_until="domcontentloaded", timeout=90000)
    _wait_designer(d, timeout)
    if not bridge:
        return cfg
    launch_bridge()
    r = js("return await webflow.getSiteInfo()")
    if not r["ok"]:
        raise RuntimeError("getSiteInfo failed: " + r["err"])
    info = r["v"] or {}
    if info.get("shortName") and info["shortName"] != cfg["shortName"]:
        raise RuntimeError(f"bridge is on {info['shortName']}, expected {cfg['shortName']}")
    saved = save_site(cfg["key"], siteId=info.get("siteId"), name=info.get("siteName"))
    relay_attach()
    return saved


def grant_local_network(url):
    """Let this URL's origin reach local and LAN addresses. Chrome ignores Browser.setPermission without an origin
    (it answers ok, the state stays denied) and grants these three permissions separately."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    s = browser.new_browser_cdp_session()
    for name in ("local-network-access", "local-network", "loopback-network"):
        try:
            s.send("Browser.setPermission", {"permission": {"name": name}, "setting": "granted", "origin": origin})
        except Exception as e:  # a Chrome version may not know one of the names
            print(f"{name} not granted for {origin}:", str(e)[:120])
    return origin


def relay_up():
    """Start relay.py if it isn't running (it outlives the driver). Returns its HTTP port."""
    import socket, subprocess, sys
    here = globals().get("HERE") or os.getcwd()
    port = int(os.environ.get("RELAY_PORT", 8770))

    def listening():
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0
    if not listening():
        log = open(os.path.join(here, "relay.out"), "a")
        detached = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, "-I", "-u", os.path.join(here, "relay.py")], cwd=here, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, **detached)
        t0 = time.time()
        while not listening():
            if time.time() - t0 > 10:
                raise RuntimeError("relay.py did not start: see relay.out")
            time.sleep(0.1)
    return port


def relay_attach(ref=None):
    """Connect this site's Designer to relay.py, so wb.py can reach it without Playwright. It stays connected through
    Designer reloads while this driver runs, and across relay restarts. Never fails the caller: returns True or False."""
    try:
        here = globals().get("HERE") or os.getcwd()
        d, port = designer(ref), relay_up()
        grant_local_network(d.url)
        token = _json.load(open(os.path.join(here, f".relay-token-{port}")))["client"]
        config = _json.dumps({"port": port + 1, "token": token, "prelude": _PRELUDE})
        src = open(os.path.join(here, "relay.js"), encoding="utf-8").read().replace("__CONFIG__", config)
        tabs = globals().setdefault("_relay_tabs", {})
        if tabs.get(id(d)) is not d:
            d.add_init_script(src)  # every future document in this tab, the bridge app frame included
            tabs[id(d)] = d
        for target in (d, bridge_frame(ref)):  # the documents already open
            target.evaluate(src)
        return True
    except Exception as e:
        print("relay not attached:", str(e)[:200])
        return False


def live(path="/", fresh=True, ref=None):
    """Open (or reuse) a tab on the site's staging URL + path; fresh adds a cache-buster. Returns the tab."""
    cfg = site(ref)
    if not cfg:
        raise RuntimeError("live(): no site; pass x.py --site <key> or call use('<key>')")
    url = cfg["staging"].rstrip("/") + "/" + path.lstrip("/")
    if fresh:
        url += ("&" if "?" in url else "?") + f"v={int(time.time())}"
    t = _tab(cfg["staging"])
    grant_local_network(url)
    t.goto(url, wait_until="load", timeout=60000)
    return t


def _cdp(tab):
    """One CDP session per tab, reused across calls in this snippet (sessions die with the snippet's tab handle)."""
    cache = globals().setdefault("_cdp_sessions", {})
    s = cache.get(id(tab))
    if s is None:
        s = cache[id(tab)] = context.new_cdp_session(tab)
    return s


def emulate(tab, width, height=900, dpr=1, mobile=False):
    """Make the page lay out at exactly width x height through CDP and check innerWidth. Playwright's
    set_viewport_size can silently stop applying (e.g. after bring_to_front: viewport_size says 390, the page still
    sees 1440). Reload or navigate afterwards so load-time layout and lazy images match. Returns innerWidth."""
    _cdp(tab).send("Emulation.setDeviceMetricsOverride",
                   {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": mobile})
    tab.wait_for_timeout(300)
    w = tab.evaluate("innerWidth")
    if w != width:
        raise RuntimeError(f"emulate({width}): page reports innerWidth {w}")
    return w


def clear_emulation(tab):
    """Undo emulate(); returns the page's innerWidth once the window size is back."""
    _cdp(tab).send("Emulation.clearDeviceMetricsOverride")
    tab.wait_for_timeout(300)
    return tab.evaluate("innerWidth")


def snap(tab, path, full=False, clip=None, quality=None):
    """Screenshot through CDP (Playwright's page/element screenshots can hang 30-100 s on Webflow pages and on the
    Designer). full=True captures the whole document; clip={'x','y','width','height'} captures a region in CSS px.
    quality (1-100) writes JPEG. Returns the path."""
    import base64
    params = {"format": "jpeg" if quality else "png"}
    if quality:
        params["quality"] = quality
    if full:
        w, h = tab.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
        params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1})
    elif clip:
        params["clip"] = dict(clip, scale=clip.get("scale", 1))
    data = _cdp(tab).send("Page.captureScreenshot", params)["data"]
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return path


def js(body, arg=None, ms=20000):
    """Run an async JS body in the bridge frame. Returns {ok, v | err, ms (in frame), rt (round trip), T, E}.
    Never raises for JS errors; an ITEM TIMEOUT after `ms` comes back as ok False too."""
    t = time.time()
    code = ("async (a) => { const t0 = performance.now();" + _PRELUDE +
            "try { const v = await (async () => {" + body + "})(); return {ok: true, v, ms: Math.round(performance.now() - t0), T, E} }"
            " catch (e) { return {ok: false, err: String(e && e.message || e).slice(0, 500), ms: Math.round(performance.now() - t0), T, E} } }")
    try:
        r = evaluate_t(bridge_frame(), code, arg, ms)
    except Budget:
        raise
    except Exception as e:
        r = {"ok": False, "err": str(e)[:500]}
        if "ITEM TIMEOUT" in r["err"]:
            try:  # keep the per-call timings gathered before the hang
                r.update(bridge_frame().evaluate("() => ({T: window.__tmT || {}, E: window.__tmE || {}})"))
            except Exception:
                pass
    r["rt"] = round((time.time() - t) * 1000)
    for k in ("T", "E"):
        if not r.get(k):
            r.pop(k, None)
    return r


def until(body, timeout=10, every=0.15, arg=None):
    """Poll a js() body until it returns something truthy. Returns (seconds, value); seconds is None on timeout."""
    t, v = time.time(), None
    while time.time() - t < timeout:
        check()
        r = js(body, arg)
        v = r.get("v")
        if r.get("ok") and v:
            return round(time.time() - t, 2), v
        designer().wait_for_timeout(int(every * 1000))
    return None, v


def switch_page(page, timeout=20, settle=1.5):
    """Switch the Designer to a page (id, name or slug) without awaiting the switch (awaiting can hang), then poll
    until it is current. Returns the seconds it took."""
    r = js("const all = (await webflow.getAllPagesAndFolders()).filter(x => !/folder/i.test(x.type || ''));"
           " let p = all.find(x => x.id === a);"
           " if (!p) { const hit = (await Promise.all(all.map(async x => ((await x.getName?.()) === a || (await x.getSlug?.()) === a) && x))).filter(Boolean);"
           "   if (hit.length > 1) throw new Error('AMBIGUOUS: ' + hit.length + ' pages match ' + a + ' (' + hit.map(x => x.id).join(', ') + '); pass an id');"
           "   p = hit[0] }"
           " if (!p) throw new Error('no page ' + a); webflow.switchPage(p); return p.id", page)
    if not r["ok"]:
        raise RuntimeError(f"switch_page: {r['err']}")
    page_id = r["v"]
    s, v = until("const c = await webflow.getCurrentComponent(); return !c && (await webflow.getCurrentPage()).id === a",
                 timeout, arg=page_id)
    if s is None:
        raise RuntimeError(f"switch_page({page}) not current after {timeout}s")
    designer().wait_for_timeout(int(settle * 1000))
    return s


def open_canvas(component_id, timeout=20, settle=1.5):
    """Open a component's canvas (fire, then poll getCurrentComponent)."""
    js("webflow.openCanvas({componentId: a}); return 1", component_id)
    s, v = until("return (await webflow.getCurrentComponent())?.id === a", timeout, arg=component_id)
    if s is None:
        raise RuntimeError(f"open_canvas({component_id}) not open after {timeout}s")
    designer().wait_for_timeout(int(settle * 1000))
    return s


_granted = set()


def clip(text):
    """Put text on the clipboard of the Designer page (grants clipboard permission for its origin once)."""
    d = designer()
    origin = "/".join(d.url.split("/")[:3])
    if origin not in _granted:
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        _granted.add(origin)
    d.evaluate("t => navigator.clipboard.writeText(t)", text)


def paste_at(x, y, text, commit="Enter"):
    """Click a field, select all, paste text, then press commit (Enter / Tab / None). Never types."""
    d = designer()
    d.mouse.click(x, y)
    d.keyboard.press("Control+A")
    clip(text)
    d.keyboard.press("Control+V")
    if commit:
        d.keyboard.press(commit)


def select(element_id):
    """Select an element by id (so the right panel shows it). Returns its type."""
    r = js("const el = await byId(a); if (!el) throw new Error('no element ' + a); await webflow.setSelectedElement(el); return el.type",
           element_id)
    if not r["ok"]:
        raise RuntimeError(r["err"])
    return r["v"]


def _bridge_of(d):
    """The live bridge app frame in Designer tab d, or None."""
    return next((f for f in reversed(d.frames) if EXT in f.url and not f.is_detached()), None)


def _launch_app(d, timeout=40):
    """The Apps panel clicks that launch the bridge app in Designer tab d, then minimize it. Returns its frame."""
    t0 = time.time()
    app = d.get_by_text("Webflow MCP Bridge App", exact=True)
    if not app.count():
        d.locator("[aria-label^='Apps']").first.click()  # left-rail Apps icon ("Apps (E)")
        d.wait_for_timeout(1500)
    app.first.click(timeout=10000)
    d.wait_for_timeout(1500)
    launch = d.get_by_role("button", name=re.compile(r"^Launch"))
    (launch if launch.count() else d.get_by_text("Launch", exact=True)).first.click(timeout=10000)
    while not _bridge_of(d):
        check()
        if time.time() - t0 > timeout:
            raise RuntimeError("bridge frame did not appear")
        d.wait_for_timeout(500)
    d.wait_for_timeout(3000)
    collapse = d.locator('button[aria-label="Collapse"]')
    if collapse.count():
        collapse.first.click()  # minimize, never close
    return _bridge_of(d)


def designer_windows(n=3, ref=None):
    """n Designer tabs of this site, each in its own Chrome window with the bridge app running, side by side and each
    in front of its window. Chrome throttles a tab that isn't in front of its window (Designer calls drop to about
    one per second), so parallel work needs windows, not tabs. Reuses open Designers; returns the tabs."""
    cfg = site(ref)
    if not cfg:
        raise RuntimeError("designer_windows(): no site; pass x.py --site <key> or call use('<key>')")
    host = _host(cfg["designer"])
    bs = browser.new_browser_cdp_session()
    win = lambda p: _cdp(p).send("Browser.getWindowForTarget")["windowId"]
    tabs = {}
    for p in context.pages:
        if _host(p.url) == host:
            tabs.setdefault(win(p), p)  # one Designer per window
    pages = list(tabs.values())
    if not pages:
        raise RuntimeError(f"DESIGNER NOT OPEN for {cfg['key']}: open_site('{cfg['key']}')")
    for p in pages:
        if not _bridge_of(p):  # e.g. after a driver restart: this client can't see the old frame
            p.reload(wait_until="domcontentloaded", timeout=90000)
            _wait_designer(p, 120)
            _launch_app(p)
    while len(pages) < n:
        progress(f"opening Designer window {len(pages) + 1} of {n}")
        known = set(id(p) for p in context.pages)
        bs.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
        end, new = time.time() + 15, []
        while not new:
            if time.time() > end:
                raise RuntimeError("new Chrome window did not appear")
            pages[0].wait_for_timeout(200)  # lets Playwright see the new page (time.sleep would not)
            new = [p for p in context.pages if id(p) not in known]
        d = new[0]
        block_media(d)
        d.goto(cfg["designer"], wait_until="domcontentloaded", timeout=90000)
        _wait_designer(d, 120)
        _launch_app(d)
        pages.append(d)
    pages = pages[:n]
    sw, sh = pages[0].evaluate("[screen.availWidth, screen.availHeight]")
    for i, p in enumerate(pages):
        w = win(p)
        bs.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "normal"}})
        bs.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"left": i * sw // n, "top": 0, "width": sw // n,
                                                                       "height": min(sh, 1000)}})
        p.bring_to_front()
    relay_attach(ref)  # a reload above drops the relay script; reconnect it (no-op if it's still there)
    return pages


def close_designer_windows(ref=None):
    """Close every Designer tab of this site except the first (the one the relay is connected to), and give its window
    the default size again."""
    cfg = site(ref)
    host = _host(cfg["designer"])
    des = [p for p in context.pages if _host(p.url) == host]
    for p in des[1:]:
        p.close()
    if des:
        bs = browser.new_browser_cdp_session()
        w = _cdp(des[0]).send("Browser.getWindowForTarget")["windowId"]
        bs.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "normal"}})
        bs.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"left": 0, "top": 0, "width": 1600, "height": 1000}})
        des[0].bring_to_front()
    relay_attach(ref)
    return len(des[1:])


_RUN_WORKER = r"""async (x) => {
  const gen = (window.__runGen = (window.__runGen || 0) + 1);
  window.__runOut = {}; window.__runDone = false; (window.__runAll = window.__runAll || {})[gen] = {};
  const read = new (async () => {}).constructor('a', x.prelude + x.body);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  (async () => {
    for (const id of x.ids) {
      if (window.__runGen !== gen) return;
      const t0 = performance.now();
      try {
        const p = (await webflow.getAllPagesAndFolders()).find(q => q.id === id);
        if (!p) throw new Error('no page ' + id);
        webflow.switchPage(p);  // never awaited: awaiting can hang
        let ok = false;
        for (let i = 0; i < 300 && !ok; i++) {
          if (window.__runGen !== gen) return;
          ok = !(await webflow.getCurrentComponent()) && (await webflow.getCurrentPage()).id === id;
          if (!ok) await sleep(100);
        }
        if (!ok) throw new Error('page not current after 30 s');
        await sleep(350);
        const v = await read(x.arg);  // finish first: `out[id] = await …` would pick the object before the await
        window.__runOut[id] = window.__runAll[gen][id] = { v, secs: Math.round(performance.now() - t0) / 1000 };
      } catch (e) {
        window.__runOut[id] = window.__runAll[gen][id] = { error: String(e && e.message || e).slice(0, 300), secs: Math.round(performance.now() - t0) / 1000 };
      }
    }
    if (window.__runGen === gen) window.__runDone = true;
  })();
  return gen;
}"""
_RUN_POLL = "async () => { const r = window.__runOut || {}; window.__runOut = {}; return { done: !!window.__runDone, r } }"
_RUN_STOP = "async () => { window.__runGen = (window.__runGen || 0) + 1; return 1 }"
_RUN_ALL = "async (g) => (window.__runAll || {})[g] || {}"


def run_pages(pages, body, out, arg=None, n=3, secs=240, ref=None):
    """Run a js()-style body on every page id, spread over n Designer windows (designer_windows). Each window runs its
    own page loop inside the Designer, so the windows work at the same time. Results go to the JSON file `out` as they
    arrive; pages already saved without an error are skipped, so call it again until remaining is 0. Stops its loops
    after `secs` seconds, counted from the call (opening windows included): give x.py a budget about 30 s above that.
    A final sweep collects every result the loops kept; recovered_in_sweep above 0 means the polling missed some."""
    t0 = time.time()
    store = _json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    todo = [p for p in pages if not (p in store and "error" not in store[p])]
    frames = []
    if todo:
        tabs = designer_windows(min(n, len(todo)), ref)
        frames = [_bridge_of(p) for p in tabs]
        gens = [evaluate_t(f, _RUN_WORKER, {"ids": todo[i::len(frames)], "body": body, "arg": arg, "prelude": _PRELUDE},
                           ms=15000) for i, f in enumerate(frames)]
    done = [False] * len(frames)
    while not all(done):
        got = 0
        for i, f in enumerate(frames):
            if done[i]:
                continue
            r = evaluate_t(f, _RUN_POLL, None, ms=15000)
            store.update(r["r"])
            got += len(r["r"])
            done[i] = r["done"]
        if got:
            _json.dump(store, open(out, "w", encoding="utf-8"), default=str)
        progress(f"{sum(1 for p in pages if p in store)}/{len(pages)} pages in {len(frames)} windows")
        if time.time() - t0 > secs:
            for f in frames:
                evaluate_t(f, _RUN_STOP, None, ms=15000)
            break
        frames[0].page.wait_for_timeout(1000)
    recovered = 0
    for f, g in zip(frames, gens if frames else []):  # final sweep: every result the loop kept
        for k, v in evaluate_t(f, _RUN_ALL, g, ms=15000).items():
            if k not in store or ("error" in store[k] and "error" not in v):
                store[k] = v
                recovered += 1
    _json.dump(store, open(out, "w", encoding="utf-8"), default=str)
    left = [p for p in pages if p not in store]
    times = [store[p]["secs"] for p in pages if p in store and "secs" in store[p]]
    return {"done_total": len(pages) - len(left), "remaining": len(left), "windows": len(frames), "recovered_in_sweep": recovered,
            "wall_s": round(time.time() - t0, 1), "avg_page_s": round(sum(times) / len(times), 2) if times else None,
            "errors": [p for p in pages if "error" in store.get(p, {})]}


def launch_bridge(timeout=40):
    """Launch the MCP Bridge App in this site's Designer (Apps panel → app → Launch) and minimize it. No-op if its
    frame is already there. Returns the seconds it took."""
    d, t0 = designer(), time.time()
    if _bridge_of(d):
        return 0
    _launch_app(d, timeout)
    r = js("return 1")
    if not r["ok"]:
        raise RuntimeError("bridge not answering: " + r["err"])
    return round(time.time() - t0, 1)


def relaunch_bridge(timeout=100):
    """Reload this site's Designer and launch the bridge again. Needed after a driver restart: a Playwright client
    only sees bridge frames created after it attached. Returns the seconds it took."""
    d, t0 = designer(), time.time()
    d.reload(wait_until="domcontentloaded", timeout=90000)
    _wait_designer(d, timeout)
    launch_bridge()
    relay_attach()
    return round(time.time() - t0, 1)
