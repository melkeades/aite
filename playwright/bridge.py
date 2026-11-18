"""Webflow Designer helpers. driver.py loads this file into every snippet's globals (and reloads it when it changes),
so snippets call these directly. Uses the driver globals context, evaluate_t, check and progress.

    open_site("ca-bps")  # or a Designer URL / sites/<key>.json key: opens or reuses its tab, launches the bridge
    r = js("const h = await byId(a); return await h.getDisplayName()", element_id)
    r = js("await tm('rename', () => h.setDisplayName('x')); return 1")   # r["T"] / r["E"]: per-call ms / errors
    switch_page(page_id); open_canvas(component_id); designer(); canvas()
    t = live("/pricing")     # one staging page in its own window, right after a change; raises if it doesn't load
    until("return (await webflow.getSelectedElement())?.type === 'Heading'")
    paste_at(1480, 751, "new text")

Sites: nothing here is tied to one Webflow project. Each site has local settings in sites/<key>.json (created on first
open_site). The site a snippet works on comes from `x.py --site <key>` (or use()/open_site() inside the snippet) and
lasts for that snippet only. Without one, the helpers work only while exactly one Designer tab is open; with several
they refuse rather than guess, so a call never lands in another project's tab.

Every js() call resolves the bridge frame again, so it survives page and canvas switches."""
import json as _json, os, random, re, time
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
    return [p for p in context.pages if _host(p.url).endswith(DESIGN) and not _owned(p)]


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


def _target_id(p):
    """Chrome's target id of a tab (cached)."""
    cache = globals().setdefault("_target_ids", {})
    t = cache.get(id(p))
    if t is None:
        t = cache[id(p)] = _cdp(p).send("Target.getTargetInfo")["targetInfo"]["targetId"]
    return t


def _owned(p):
    """True for a kept session window (ops.py): shared-tab helpers never use those."""
    ids = globals().get("OWNED")
    if not ids:
        return False
    try:
        return _target_id(p) in ids
    except Exception:
        return False


def _shared_pages():
    return [p for p in context.pages if not _owned(p)]


def _tab(url):
    """A tab already on url's host, else a blank one or a new one. A Designer tab gets video blocked before it
    navigates; every other tab plays video normally. Never a window a Designer job owns, nor the live() window."""
    h = _host(url)
    lives = list(globals().get("_live_tabs", {}).values())
    shared = [p for p in _shared_pages() if p not in lives]
    tab = (next((p for p in shared if _host(p.url) == h), None)
           or next((p for p in shared if p.url == "about:blank"), None) or context.new_page())
    block_media(tab if h.endswith(DESIGN) else None)
    return tab


def open_site(ref, bridge=True, timeout=120):
    """Open (or reuse) a site's Designer and make it this snippet's site. ref: sites/<key>.json key, short name, or
    Designer URL (an unknown site gets a sites/<short>.json). Launches the bridge, then records the site's id and
    name from webflow.getSiteInfo() and checks the bridge belongs to this site. Returns its settings."""
    cfg = use(ref)
    d = _tab(cfg["designer"])
    if _host(d.url) != _host(cfg["designer"]):
        _log(f"designer: open_site('{cfg['key']}') opened the main Designer tab (it stays open)")
        d.goto(cfg["designer"], wait_until="domcontentloaded", timeout=90000)
    else:
        _log(f"designer: open_site('{cfg['key']}') reused the open Designer tab")
    _wait_designer(d, timeout)
    if not bridge:
        return cfg
    try:
        launch_bridge()
    except Budget:
        raise
    except Exception as e:
        # typically after a driver restart: the tab is open but this client can't reach its bridge app. Reload once.
        _log(f"designer: open_site('{cfg['key']}') could not launch the bridge ({str(e).splitlines()[0][:80]}); reloading the Designer tab once")
        relaunch_bridge()
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


def _new_window(tag):
    """A new Chrome window with one blank tab, made by this Playwright client and ready to navigate."""
    bs = browser.new_browser_cdp_session()
    try:
        # background: the window opens without taking the keyboard focus from the user
        bs.send("Target.createTarget", {"url": tag, "newWindow": True, "background": True, "width": 1500, "height": 1000})
        end, t = time.time() + 15, None
        while t is None:
            check()
            if time.time() > end:
                raise RuntimeError("new Chrome window did not appear")
            bs.send("Browser.getVersion")  # a round trip, so Playwright processes the new-page event
            t = next((p for p in context.pages if p.url == tag), None)
        t.evaluate("1")  # Playwright finishes setting the tab up before it navigates
        return t
    finally:
        bs.detach()


def live(path="/", fresh=True, ref=None):
    """One page of the site's staging URL, for a look right after a Designer change inside the same snippet. It gets a
    window of its own (a tab beside the Designer is not painted, so captures and hovers stall there) and is reused by
    later calls. Raises RuntimeError if the page does not load (HTTP >= 400, network error, no load event in 60 s).
    Several pages, several widths, or anything that should not hold the driver: live.py."""
    cfg = site(ref)
    if not cfg:
        raise RuntimeError("live(): no site; pass x.py --site <key> or call use('<key>')")
    url = cfg["staging"].rstrip("/") + "/" + path.lstrip("/")
    if fresh:
        url += ("&" if "?" in url else "?") + f"v={int(time.time())}"
    tabs = globals().setdefault("_live_tabs", {})
    t = tabs.get(cfg["key"])
    if t is None or t.is_closed():
        t = tabs[cfg["key"]] = _new_window(f"about:blank#live-{cfg['key']}")
    grant_local_network(url)
    try:
        resp = t.goto(url, wait_until="load", timeout=60000)
    except Exception as e:
        raise RuntimeError(f"LOAD FAILED: {str(e).splitlines()[0][:200]} {url}")
    status = resp.status if resp else None
    if status is None or status >= 400:
        raise RuntimeError(f"LOAD FAILED: HTTP {status} {url}")
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
           " if (!p) throw new Error('no page ' + a); return p.id", page)
    if not r["ok"]:
        raise RuntimeError(f"switch_page: {r['err']}")
    page_id = r["v"]
    r = js("const p = (await webflow.getAllPagesAndFolders()).find(x => x.id === a); webflow.switchPage(p); return 1", page_id)
    if not r["ok"]:
        raise RuntimeError(f"switch_page: {r['err']}")
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


def _wait_designers(tabs, timeout=120):
    """_wait_designer for several tabs at once: one poll loop over all of them."""
    t0, left = time.time(), list(tabs)
    while left:
        check()
        left = [d for d in left if not (d.get_by_text("Style", exact=True).count() and d.get_by_text("Settings", exact=True).count())]
        if left:
            if time.time() - t0 > timeout:
                raise RuntimeError(f"{len(left)} Designer(s) did not load")
            tabs[0].wait_for_timeout(500)
    tabs[0].wait_for_timeout(2000)


def _launch_apps(tabs, timeout=60):
    """_launch_app for several Designer tabs at once: every step runs in each tab back to back, with one wait per
    step, so n windows take about as long as one (one by one, setup took about 40 s for 3 windows)."""
    t0 = time.time()
    todo = [d for d in tabs if not _bridge_of(d)]
    if todo:
        for d in todo:
            if not d.get_by_text("Webflow MCP Bridge App", exact=True).count():
                d.locator("[aria-label^='Apps']").first.click()  # left-rail Apps icon ("Apps (E)")
        todo[0].wait_for_timeout(1500)
        for d in todo:
            d.get_by_text("Webflow MCP Bridge App", exact=True).first.click(timeout=10000)
        todo[0].wait_for_timeout(1500)
        for d in todo:
            launch = d.get_by_role("button", name=re.compile(r"^Launch"))
            (launch if launch.count() else d.get_by_text("Launch", exact=True)).first.click(timeout=10000)
        while not all(_bridge_of(d) for d in todo):
            check()
            if time.time() - t0 > timeout:
                raise RuntimeError(f"bridge frame did not appear in {sum(1 for d in todo if not _bridge_of(d))} window(s)")
            todo[0].wait_for_timeout(300)
        todo[0].wait_for_timeout(3000)
        for d in todo:
            collapse = d.locator('button[aria-label="Collapse"]')
            if collapse.count():
                collapse.first.click()  # minimize, never close
    return [_bridge_of(d) for d in tabs]


MIN_WINDOW_W = 1100  # the Designer needs at least 900 px; narrower windows get "Your browser is too small"


def tile_windows(pages, min_width=MIN_WINDOW_W):
    """Spread windows across the screen, each at least min_width wide, overlapping when the screen is too narrow for
    side by side (one per third of a 2560 px screen is 853 px: Webflow then covers the Designer with "Your browser is
    too small"). Each is brought to front of its own window. Returns each window's innerWidth."""
    if not pages:
        return []
    bs = browser.new_browser_cdp_session()
    wids = [_cdp(p).send("Browser.getWindowForTarget")["windowId"] for p in pages]
    # The real work area, from Windows itself (the page's screen.availWidth said 2560 where the work area was about
    # 2040, and Chrome shrank windows placed past the edge). Never by maximizing a window: that activates it, and an
    # activated window on another virtual desktop pulls the user over there.
    import ctypes

    class _RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    rc = _RECT()
    ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
    x0, y0, sw, sh = rc.l, rc.t, (rc.r - rc.l) or 1920, (rc.b - rc.t) or 1040
    n = len(pages)
    w = min(sw, max(min_width, sw // n))
    step = (sw - w) // (n - 1) if n > 1 else 0
    for wid in wids:
        bs.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
    pages[0].wait_for_timeout(500)  # a restore lands after the call returns and would undo a resize sent right away
    want = [{"left": max(0, x0) + i * step, "top": max(0, y0), "width": w, "height": min(sh, 1000)} for i in range(n)]
    for attempt in range(3):
        for p, wid, b in zip(pages, wids, want):
            bs.send("Browser.setWindowBounds", {"windowId": wid, "bounds": b})
        pages[0].wait_for_timeout(300)
        if all(bs.send("Browser.getWindowBounds", {"windowId": wid})["bounds"]["width"] == b["width"]
               for wid, b in zip(wids, want)):
            break
    # No bring_to_front: every job window has one tab, which is always the front tab of its window, and activating a
    # window that sits on another virtual desktop switches the user to that desktop.
    return [p.evaluate("innerWidth") for p in pages]


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


# ---- kept Designer windows: the session wf.py's operations (ops.py) work in ----
MAX_JOB_WINDOWS = 6
JOB_KEEP = int(os.environ.get("JOB_KEEP", 300))  # seconds a Designer window stays open, idle, for the next command
def _pool():
    """Designer windows that outlive the command that opened them, per site: {site key: [{"tab", "tid", "last",
    "job"}]}. They stay open with their bridge app running for JOB_KEEP seconds after use, so the next command (the
    write after a read, the QA after a write) starts at once instead of opening and loading windows again."""
    return globals().setdefault("JOBWIN", {})


def _log(msg):
    fn = globals().get("activity")
    if fn:
        fn(msg)


def _pool_ok(w):
    """True if a kept window is still usable: open, on the Designer, bridge app answering."""
    try:
        d = w["tab"]
        if d.is_closed():
            return False
        f = _bridge_of(d)
        return bool(f) and evaluate_t(f, "async () => 1", None, ms=4000) == 1
    except Exception:
        return False


def _pool_close(w):
    try:
        if not w["tab"].is_closed():
            w["tab"].close()
    except Exception:
        pass
    OWNED.discard(w["tid"])


def job_windows_idle(close_all=False):
    """Close kept Designer windows nobody used for JOB_KEEP seconds (all idle ones with close_all). The driver calls
    this between snippets; wf.py close calls it with close_all. Returns how many it closed."""
    n = 0
    for key, wins in list(_pool().items()):
        for w in list(wins):
            if w["job"] is None and (close_all or time.time() - w["last"] > JOB_KEEP):
                _pool_close(w)
                wins.remove(w)
                n += 1
        if not wins:
            _pool().pop(key, None)
    if n:
        _log(f"designer: closed {n} idle window(s)" + ("" if close_all else f" (unused for {JOB_KEEP}s)"))
    return n


def job_windows_status():
    return [{"site": k, "open": len(v), "busy": sum(1 for w in v if w["job"]),
             "idle_s": [round(time.time() - w["last"]) for w in v if not w["job"]]} for k, v in _pool().items()]


def _job_new_window(cfg, tag):
    """A new Chrome window for the session, already navigating to the site's Designer (video blocked first)."""
    d = _new_window(tag)
    tid = _target_id(d)
    OWNED.add(tid)
    block_media(d)
    _cdp(d).send("Page.navigate", {"url": cfg["designer"]})
    return d, tid
