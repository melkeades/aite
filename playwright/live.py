"""Live pages (staging, production, any public URL), always in parallel. No driver, no Designer, no shared Chrome.

A run takes N Chrome windows (default 3), hands the items to whichever window is free and saves each result as it
arrives. All N windows work at the same time; there is no way to drive them one after another.

The windows belong to a small keeper process, not to the run. They open for the first run and STAY OPEN, so the next
run (the follow-up question, the re-check after a fix) starts in the same windows at once instead of opening Chromes
again. The keeper closes windows nobody has used for KEEP seconds (default 300; `keep_s` in machine.json) and then
exits. Stopping a run (Ctrl+C, a stopped tool call, `live.py --stop`) stops its work at once and leaves the windows
for the next run; `live.py --close` closes them now.

    python live.py --site KEY check.py                      # check.py defines each(p, item) and ITEMS
    python live.py --site KEY --js scan.js /a /b /c          # run one JS file on every page, save what it returns
    python live.py --site KEY --shots DIR --widths 1440,390 /a /b   # full-page captures at each width
    python live.py --status | --stop [PID] | --close

A script (check.py) is plain Python. It only says what to do on ONE loaded page; the loop, the windows, saving,
resuming, time limits and errors are handled here:

    ITEMS = ["/", "/pricing", {"path": "/about", "width": 390}]     # paths (site staging URL + path) or full URLs
    OUT = r"C:\\tmp\\results.json"                                   # optional; also WINDOWS, WIDTH, HEIGHT, BUDGET,
                                                                    # PER_PAGE, DEV, WAIT, FRESH, LOAD_TIMEOUT, SITE
                                                                    # (the command line wins)
    def each(p, item):            # p: the loaded page, alone in its own window. Runs in every window at once.
        r = p.evaluate("() => ({h1: document.querySelector('h1')?.innerText})")
        p.snap(rf"C:\\tmp\\{r['h1']}.jpg", full=True, quality=75)
        return r                  # saved under the item's key in OUT

    def done(results, summary):   # optional, once at the end (build the report here)
        ...

`p` is a Playwright page (p.mouse, p.locator(...), p.keyboard, p.on("console", fn) all work) with these on top:
    p.goto(target)       load another page; raises LoadError (HTTP >= 400, network error, no load event in time)
    p.evaluate(js, arg, timeout=30)   a function (called with arg) or an expression; raises after timeout seconds
    p.snap(path, full=False, clip=None, quality=None)   capture through CDP; clip is in document CSS px
    p.emulate(width, height=None)     lay out at exactly this width, then p.reload(); raises if innerWidth disagrees
    p.load_all()         scroll through the page so lazy images and scroll animations run, then back to the top
    p.wait(ms), p.note(msg), p.status (HTTP status of the last load), p.item, p.width

Errors are never swallowed:
- a page that does not load raises LoadError. For an item, it is recorded as {"error": ...}, printed, and listed in
  the summary; the other items go on. In your own p.goto() it is an exception you can catch.
- a page that takes longer than PER_PAGE seconds (default 60) has its window killed and reopened; the item is an error.
- at the end, run() raises PagesFailed if any item failed (results are saved first); the command exits 1.
- nothing is cut: every item's whole result is in OUT. Read the file; don't run again for something it already has.
- BUDGET (default 240 s) stops the run cleanly with items left: exit 4, run the same command again to continue. Items
  already saved in OUT without an error are skipped: give every task its own OUT. When nothing is left to do the run
  says NOTHING TO DO and names the file; --fresh ignores what OUT already has.
Exit codes: 0 ok, 1 items failed or script error, 3 watchdog STOP / Chrome cannot start, 4 budget used up with items
left, 5 too many live windows open across all runs (LIVE_MAX_WINDOWS, default 6).

From another script: `sys.path.insert(0, r"...\\playwright"); from live import run, LoadError, PagesFailed`, then
`summary = run("my-site", ITEMS, each, out=...)`; summary["results"] has every saved item."""
import base64, ctypes, json, os, queue, re, subprocess, sys, threading, time, traceback
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(HERE, "sites")
RUNS = os.path.join(HERE, "runs")
STOP = os.path.join(HERE, "STOP")
MACHINE = os.path.join(HERE, "machine.json")
MARK = "--aite-live"  # watchdog.py recognises this run's Chromes by it
MAX_WINDOWS = 4       # per run
CAP = int(os.environ.get("LIVE_MAX_WINDOWS", 6))  # across all runs on this machine
MEDIA = ["*.mp4*", "*.webm*", "*.mov*", "*.m3u8*", "*.ogv*"]
# Never write to Webflow from a check: form posts, the API, the dashboard and the Designer are blocked in every window.
WRITE_BLOCK = ["*://webflow.com/api/*", "*://*.webflow.com/api/*", "*://api.webflow.com/*", "*://webflow.com/dashboard*",
               "*.design.webflow.com*"]
CHROME_ARGS = ["--mute-audio", "--no-first-run", "--no-default-browser-check", "--disable-accelerated-video-decode",
               "--autoplay-policy=user-gesture-required", "--disable-background-timer-throttling",
               "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
               "--disable-features=CalculateNativeWinOcclusion", "--hide-crash-restore-bubble", MARK]
KEEPER = os.path.join(RUNS, "keeper.json")
ACTIVITY = os.path.join(HERE, "activity.log")


def activity(msg):
    """One line per thing that opens, reuses or closes windows (activity.log, shared with the driver)."""
    try:
        with open(ACTIVITY, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass
DEV_INIT = "document.devMode = true"  # the user's convention: the site then loads the local Vite build


class LoadError(Exception):
    """The page did not load: HTTP status >= 400, a network error, or no load event in time."""
    def __init__(self, msg, url=None, status=None):
        super().__init__(msg)
        self.url, self.status = url, status


class PagesFailed(Exception):
    """Raised by run() after everything is saved, when at least one item failed. .summary has the details."""
    def __init__(self, summary):
        super().__init__(f"{len(summary['errors'])} of {summary['total']} items failed: "
                         + "; ".join(f"{k}: {v}" for k, v in list(summary["errors"].items())[:5]))
        self.summary = summary


class Busy(Exception):
    """Too many live windows are open across all runs."""


class StackDown(Exception):
    """The watchdog stopped Chrome, the watchdog isn't running, or Chrome cannot start."""


# ---- machine and site settings ----

def machine():
    """machine.json: {"chrome_args": [...GPU pinning...], "watchdog": true}. Required, so a run can never start Chrome
    on the wrong GPU by accident."""
    if not os.path.exists(MACHINE):
        raise StackDown(f"NO {MACHINE}: copy machine.example.json and set this machine's GPU flags (README 'Start')")
    return json.load(open(MACHINE, encoding="utf-8"))


def check_stack(cfg):
    if os.path.exists(STOP):
        raise StackDown("WATCHDOG STOPPED CHROME: " + open(STOP).read().strip())
    if cfg.get("watchdog", True):
        log = os.path.join(HERE, "gpu.log")
        if not os.path.exists(log) or time.time() - os.path.getmtime(log) > 20:
            raise StackDown("WATCHDOG NOT RUNNING (gpu.log is stale): start it with the README 'Start' command")


def site_cfg(ref):
    """Settings of a site by sites/<key>.json key, short name, site id, name or a Designer/staging URL."""
    if not ref:
        return None
    m = re.match(r"^(?:https?://)?([a-z0-9-]+)\.(?:design\.webflow\.com|webflow\.io)", ref.strip().lower())
    short = m.group(1) if m else ref.strip()
    if os.path.isdir(SITES):
        for f in sorted(os.listdir(SITES)):
            if f.endswith(".json") and f != "example.json":
                cfg = json.load(open(os.path.join(SITES, f), encoding="utf-8"))
                if ref in (f[:-5], cfg.get("siteId"), cfg.get("name")) or short == cfg.get("shortName"):
                    cfg.setdefault("staging", f"https://{cfg.get('shortName', f[:-5])}.webflow.io/")
                    return dict(cfg, key=f[:-5])
    if m or re.fullmatch(r"[a-z0-9-]+", short):
        return {"key": short, "shortName": short, "staging": f"https://{short}.webflow.io/"}
    raise ValueError(f"UNKNOWN SITE {ref!r}: use a sites/<key>.json key, a short name, or a Designer/staging URL")


def _app_host(h):
    h = (h or "").lower()
    return h == "webflow.com" or h.endswith(".webflow.com")  # Designer, dashboard, API; staging is *.webflow.io


# ---- one page in one window ----

class Tab:
    """The page of one live window: a Playwright page plus checked loads, timed evaluates and CDP captures."""
    def __init__(self, page, win):
        self.page, self._win, self._cdp_s, self._on = page, win, None, []
        self.status, self.item, self.width, self.height = None, None, win.run.width, win.run.height

    def __getattr__(self, k):
        return getattr(self.page, k)

    def _cdp(self):
        if self._cdp_s is None:
            self._cdp_s = self.page.context.new_cdp_session(self.page)
        return self._cdp_s

    def url_for(self, target):
        if "//" in target:
            return target
        cfg = self._win.run.site
        if not cfg:
            raise ValueError(f"no site for the path {target!r}: pass --site KEY or use full URLs")
        return cfg["staging"].rstrip("/") + "/" + target.lstrip("/")

    def goto(self, target, wait=None, timeout=None, fresh=None):
        """Load a path of the site or a full URL. Raises LoadError unless the document answers below 400 and the
        `wait` event ("load", "domcontentloaded" or "commit") fires within timeout seconds."""
        run = self._win.run
        wait, timeout = wait or run.wait, timeout or run.load_timeout
        url = self.url_for(target)
        u = urlparse(url)
        if u.scheme not in ("http", "https") or _app_host(u.hostname):
            raise LoadError(f"REFUSED: {url} is a Webflow app URL or not http(s); live.py is for published pages", url)
        if run.fresh if fresh is None else fresh:
            url += ("&" if "?" in url else "?") + f"v={int(time.time())}"
        if run.dev:
            self._win.grant_local_network(url)
        return self._load(lambda: self.page.goto(url, wait_until=wait, timeout=timeout * 1000), url, wait, timeout)

    def reload(self, wait=None, timeout=None):
        run = self._win.run
        wait, timeout = wait or run.wait, timeout or run.load_timeout
        return self._load(lambda: self.page.reload(wait_until=wait, timeout=timeout * 1000), self.page.url, wait, timeout)

    def _load(self, nav, url, wait, timeout):
        from playwright.sync_api import TimeoutError as PWTimeout, Error as PWError
        self.status = None
        try:
            resp = nav()
        except PWTimeout:
            # no evaluate here: on a document that is still loading it waits for the navigation and hangs with it
            raise LoadError(f"LOAD TIMEOUT: no '{wait}' event in {timeout}s {url}", url)
        except PWError as e:
            if self._win.killed:
                raise
            raise LoadError(f"LOAD FAILED: {str(e).splitlines()[0][:200]} {url}", url)
        self.status = resp.status if resp else None
        if self.status is None or self.status >= 400:
            raise LoadError(f"LOAD FAILED: HTTP {self.status} {url}", url, self.status)
        iw = self.page.evaluate("innerWidth")
        if iw != self.width:
            raise LoadError(f"LAYOUT WIDTH is {iw}, expected {self.width} {url}", url, self.status)
        return self

    def evaluate(self, js, arg=None, timeout=30):
        """A function expression (called with arg) or a plain expression; promises are awaited. Raises after
        `timeout` seconds instead of hanging."""
        ms = int(timeout * 1000)
        expr = (f"(async () => {{ const __f = ({js}\n); const __r = typeof __f === 'function' ? __f({json.dumps(arg)}) : __f;"
                f" let __h; try {{ return await Promise.race([__r, new Promise((_, r) => {{ __h = setTimeout(() =>"
                f" r(new Error('EVALUATE TIMEOUT {ms}ms')), {ms}) }})]) }} finally {{ clearTimeout(__h) }} }})()")
        return self.page.evaluate(expr)

    def wait(self, ms):
        self.page.wait_for_timeout(ms)

    def note(self, msg):
        """What this window is doing; shown by `live.py --status`."""
        self._win.note = str(msg)[:200]

    def on(self, event, fn):
        """page.on(...), removed again before the next item."""
        self._on.append((event, fn))
        self.page.on(event, fn)

    def _reset(self):
        for event, fn in self._on:
            try:
                self.page.remove_listener(event, fn)
            except Exception:
                pass
        self._on = []

    def emulate(self, width, height=None):
        """Lay the page out at exactly width x height (reload afterwards, so load-time layout and lazy images match).
        Raises if the page reports another innerWidth."""
        self.width, self.height = width, height or self.height
        self.page.set_viewport_size({"width": self.width, "height": self.height})
        self.page.wait_for_timeout(300)
        iw = self.page.evaluate("innerWidth")
        if iw != width:
            raise RuntimeError(f"emulate({width}): page reports innerWidth {iw}")
        return iw

    def load_all(self, step=700, pause=90):
        """Scroll through the whole page so lazy images load and scroll animations run, then return to the top."""
        self.evaluate("""async ([step, pause]) => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const go = y => window.lenis ? window.lenis.scrollTo(y, {immediate: true, force: true}) : scrollTo(0, y);
          const H = () => document.documentElement.scrollHeight;
          for (let y = 0; y < H(); y += step) { go(y); await sleep(pause); }
          go(H()); await sleep(300);
          await Promise.all([...document.images].filter(i => !i.complete).map(i => new Promise(r => { i.onload = i.onerror = r; setTimeout(r, 4000) })));
          go(0); await sleep(400);
        }""", [step, pause], timeout=60)

    def snap(self, path, full=False, clip=None, quality=None):
        """Capture through CDP. full=True: the whole document; clip={'x','y','width','height'} in document CSS px;
        quality (1-100) writes JPEG, otherwise PNG. Returns the path."""
        params = {"format": "jpeg" if quality else "png"}
        if quality:
            params["quality"] = quality
        if full:
            w, h = self.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
            params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1})
        elif clip:
            vh = self.page.evaluate("innerHeight")
            params["clip"] = dict(clip, scale=clip.get("scale", 1))
            params["captureBeyondViewport"] = clip.get("height", 0) > vh
        data = self._cdp().send("Page.captureScreenshot", params)["data"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path


class _Window:
    """One kept Chrome (one window, one page), driven by this run over CDP."""
    def __init__(self, pw, slot, run):
        self.run, self.slot, self.killed, self.note = run, slot, False, ""
        self.item, self.deadline, self.ready = None, None, threading.Event()
        info = run.slots[slot]
        self.pid, self.port = info["pid"], info["port"]
        try:
            self.browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}", timeout=8000)
        except Exception:
            # the last run left this window in a state that can't be attached (e.g. killed mid-load): replace it once
            info = run.slots[slot] = _keeper("/restart", {"pid": os.getpid(), "slot": info["slot"]})["slot"]
            self.pid, self.port = info["pid"], info["port"]
            self.browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}", timeout=15000)
        self._bs = self.browser.new_browser_cdp_session()
        self.ctx = self.browser.contexts[0]
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        page = pages[0] if pages else self.ctx.new_page()
        for extra in pages[1:]:
            extra.close()
        page.set_viewport_size({"width": run.width, "height": run.height})
        init = "\n".join(x for x in (DEV_INIT if run.dev else None, run.init_js) if x)
        if init:
            page.add_init_script(init)
        self.tab = Tab(page, self)
        c = self.tab._cdp()
        c.send("Network.enable")
        c.send("Network.setBlockedURLs", {"urls": MEDIA + WRITE_BLOCK})
        if run.dev:
            c.send("Security.setIgnoreCertificateErrors", {"ignore": True})
        try:
            self._bs.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        except Exception:
            pass
        self._ctx_id = c.send("Target.getTargetInfo")["targetInfo"].get("browserContextId")
        self._granted = set()

    def grant_local_network(self, url):
        """Let this origin reach local and LAN addresses (the local Vite build) without Chrome's prompt."""
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin in self._granted:
            return
        self._granted.add(origin)
        for name in ("local-network-access", "local-network", "loopback-network"):
            try:
                self._bs.send("Browser.setPermission", {"permission": {"name": name}, "setting": "granted", "origin": origin})
            except Exception:
                pass  # a Chrome version may not know one of the names

    def kill(self):
        """A page hung this window: have the keeper replace its Chrome. The pending call in the worker then raises."""
        self.killed = True
        try:
            self.run.slots[self.slot] = _keeper("/restart", {"pid": os.getpid(), "slot": self.run.slots[self.slot]["slot"]})["slot"]
        except Exception as e:
            self.run.fatal = self.run.fatal or f"keeper could not replace window {self.slot + 1}: {e}"
        finally:
            self.ready.set()  # the worker may connect to the replacement now"

    def close(self):
        """Let go of the window; it stays open for the next run."""
        try:
            self.tab._reset()
            if not self.killed:
                try:  # leave the window quiet for the next run: nothing loading, nothing playing
                    self.tab._cdp().send("Page.stopLoading")
                    self.tab.page.goto("about:blank", timeout=3000)
                except Exception:
                    pass
            self.browser.close()  # for a CDP connection this only disconnects
        except Exception:
            pass


# ---- the keeper: the process that owns the Chromes ----

def _keeper_state():
    try:
        k = json.load(open(KEEPER, encoding="utf-8"))
        return k if _alive(k["pid"]) else None
    except Exception:
        return None


def _keeper(path, body=None, timeout=60):
    """Call the keeper (starting it if needed for anything but a status or close call)."""
    import urllib.request
    k = _keeper_state()
    if not k:
        if path in ("/status", "/close"):
            return {"slots": [], "down": True}
        os.makedirs(RUNS, exist_ok=True)
        log = open(os.path.join(HERE, "keeper.out"), "a")
        subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__), "--keeper"], cwd=HERE, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200 | 0x08000000)
        end = time.time() + 10
        while not k and time.time() < end:
            time.sleep(0.1)
            k = _keeper_state()
        if not k:
            raise StackDown("the live.py keeper did not start: see keeper.out")
    req = urllib.request.Request(f"http://127.0.0.1:{k['port']}{path}", data=json.dumps(body or {}).encode(),
                                 method="POST", headers={"X-Token": k["token"]})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _chrome_exe(cfg):
    if cfg.get("chrome"):
        return cfg["chrome"]
    import winreg
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            return winreg.QueryValue(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
        except OSError:
            pass
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def keeper_main():
    """The keeper process. Owns every live Chrome (they die with it, through a job object), lends them to runs and
    closes the ones left idle. One per machine; it exits when it has no windows left."""
    import secrets, shutil, win32job, win32api, win32con
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    cfg = machine()
    keep = int(os.environ.get("LIVE_KEEP", cfg.get("keep_s", 300)))
    token, lock, slots, last_any = secrets.token_urlsafe(24), threading.Lock(), {}, [time.time()]
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    exe, area = _chrome_exe(cfg), _work_area()

    def launch(slot):
        prof = os.path.join(RUNS, "profiles", str(slot))
        shutil.rmtree(prof, ignore_errors=True)
        os.makedirs(prof, exist_ok=True)
        x0, y0, sw, sh = area
        w = max(520, sw // 3)
        args = [exe] + CHROME_ARGS + list(cfg.get("chrome_args") or []) + [
            f"--user-data-dir={prof}", "--remote-debugging-port=0", f"--window-position={x0 + (slot % 3) * ((sw - w) // 2)},{y0 + (slot // 3) * 40}",
            f"--window-size={w},{min(sh, 1000)}", "about:blank"]
        si = subprocess.STARTUPINFO()  # shown, never activated: the user's keyboard focus stays where it is
        si.dwFlags, si.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 4  # SW_SHOWNOACTIVATE
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=si)
        h = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, proc.pid)
        win32job.AssignProcessToJobObject(job, h)
        return {"slot": slot, "proc": proc, "pid": proc.pid, "prof": prof, "port": None, "holder": None, "last": time.time()}

    def wait_port(s, timeout=20):
        f, end = os.path.join(s["prof"], "DevToolsActivePort"), time.time() + timeout
        while time.time() < end:
            try:
                s["port"] = int(open(f).read().split()[0])
                return s
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(f"Chrome for window {s['slot'] + 1} did not open its debugging port")

    def kill(s):
        try:
            s["proc"].kill()
        except Exception:
            pass

    def public(s):
        return {"slot": s["slot"], "pid": s["pid"], "port": s["port"], "holder": s["holder"], "idle_s": round(time.time() - s["last"])}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.headers.get("X-Token") != token or "Origin" in self.headers:
                self.send_response(403)
                self.end_headers()
                return
            try:
                out = self.handle_call(body)
            except Exception as e:
                out = {"error": str(e)[:300]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_call(self, b):
            last_any[0] = time.time()
            if self.path == "/status":
                with lock:
                    return {"slots": [public(s) for s in slots.values()], "keep_s": keep, "pid": os.getpid()}
            if self.path == "/close":
                with lock:
                    n = len(slots)
                    for s in slots.values():
                        kill(s)
                    slots.clear()
                activity(f"live: closed {n} window(s) (live.py --close)")
                return {"closed": n}
            if self.path == "/release":
                with lock:
                    for s in slots.values():
                        if s["holder"] == b["pid"]:
                            s["holder"], s["last"] = None, time.time()
                return {"ok": True}
            if self.path == "/restart":
                with lock:
                    old = slots[b["slot"]]
                    kill(old)
                    s = slots[b["slot"]] = launch(b["slot"])
                    s["holder"] = b["pid"]
                wait_port(s)
                activity(f"live: window {b['slot'] + 1} replaced (a page hung it)")
                return {"slot": public(s)}
            if self.path == "/acquire":
                with lock:
                    for s in list(slots.values()):  # a Chrome that died, or whose run is gone
                        if s["proc"].poll() is not None:
                            slots.pop(s["slot"])
                        elif s["holder"] and not _alive(s["holder"]):
                            s["holder"], s["last"] = None, time.time()
                    free = [s for s in slots.values() if s["holder"] is None]
                    held = len(slots) - len(free)
                    n = min(b["n"], b["cap"] - held)
                    if n < 1:
                        return {"busy": True, "held": held}
                    mine, opened = free[:n], []
                    for i in range(b["cap"]):
                        if len(mine) + len(opened) >= n:
                            break
                        if i not in slots:
                            slots[i] = launch(i)
                            opened.append(slots[i])
                    for s in mine + opened:
                        s["holder"], s["last"] = b["pid"], time.time()
                for s in opened:
                    wait_port(s)
                activity(f"live: run {b['pid']} '{b.get('label')}': opened {len(opened)} window(s), reused {len(mine)}")
                return {"slots": [public(s) for s in mine + opened], "opened": len(opened), "reused": len(mine)}
            return {"error": "unknown call"}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.makedirs(RUNS, exist_ok=True)
    with open(KEEPER + ".tmp", "w") as f:
        json.dump({"pid": os.getpid(), "port": srv.server_address[1], "token": token}, f)
    os.replace(KEEPER + ".tmp", KEEPER)
    try:
        import desk
        desk.ensure()
    except Exception:
        pass
    print(f"keeper up on {srv.server_address[1]}, keep {keep}s", flush=True)
    try:
        while True:
            time.sleep(1)
            with lock:
                if os.path.exists(STOP):
                    for s in slots.values():
                        kill(s)
                    slots.clear()
                    break
                for s in list(slots.values()):
                    if s["proc"].poll() is not None and s["holder"] is None:
                        slots.pop(s["slot"])
                    elif s["holder"] and not _alive(s["holder"]):
                        s["holder"], s["last"] = None, time.time()
                    elif s["holder"] is None and time.time() - s["last"] > keep:
                        kill(s)
                        slots.pop(s["slot"])
                        activity(f"live: closed window {s['slot'] + 1} (unused for {keep}s)")
                if not slots and time.time() - last_any[0] > 20:
                    break
    finally:
        try:
            os.remove(KEEPER)
        except OSError:
            pass
    return 0


def _kill_tree(pid):
    if pid:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _alive(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def _work_area():
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    r = RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        if r.r - r.l > 400:
            return r.l, r.t, r.r - r.l, r.b - r.t
    except Exception:
        pass
    return 0, 0, 1920, 1040


# ---- the registry of running runs (runs/<pid>.json): status, stop and the window cap ----

def live_runs():
    out = []
    if os.path.isdir(RUNS):
        for f in os.listdir(RUNS):
            if not re.fullmatch(r"\d+\.json", f):  # keeper.json and the profiles folder live here too
                continue
            path = os.path.join(RUNS, f)
            try:
                r = json.load(open(path, encoding="utf-8"))
                if _alive(r["pid"]):
                    out.append(r)
                else:
                    os.remove(path)
            except Exception:
                pass
    return sorted(out, key=lambda r: r["started"])


def _run_line(r):
    return (f"live run {r['pid']} '{r['label']}' [{r.get('site') or 'urls'}]: {r['done']}/{r['total']} items, "
            f"{r['errors']} errors, {r['windows']} windows, {time.time() - r['started']:.0f}s of {r['budget']}s"
            + (f"; {r['last']}" if r.get("last") else ""))


def stop_runs(pid=None):
    """Kill live runs (all, or one by pid). Their windows stay open for the next run. Returns the pids stopped."""
    hit = [r for r in live_runs() if pid in (None, r["pid"])]
    for r in hit:
        _kill_tree(r["pid"])
        try:
            os.remove(os.path.join(RUNS, f"{r['pid']}.json"))
        except OSError:
            pass
        activity(f"live: run {r['pid']} '{r['label']}' stopped (live.py --stop); its windows stay open")
    return [r["pid"] for r in hit]


# ---- the run ----

class _Item:
    def __init__(self, raw, default_width):
        self.raw = raw
        if isinstance(raw, dict):
            self.target = raw.get("url") or raw.get("path")
            self.width = raw.get("width")
            self.key = str(raw.get("key") or (f"{self.target}@{self.width}" if self.width else self.target))
        else:
            self.target, self.width, self.key = str(raw), None, str(raw)
        if not self.target:
            raise ValueError(f"item {raw!r} has no path or url")
        self.width = self.width or default_width


class _Run:
    def __init__(self, **k):
        self.__dict__.update(k)
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.times, self.slow, self.done_now = [], [], 0
        self.stopping, self.fatal, self.t0 = None, None, time.time()
        self.wins = {}

    def next(self):
        """The next item for a free window, or None when the run is over or out of time."""
        with self.lock:
            if self.stopping or self.fatal:
                return None
            est = sum(self.times) / len(self.times) if self.times else 0
            if time.time() - self.t0 + est > self.budget:
                self.stopping = "budget"
                return None
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def save(self, key, rec, slot, secs):
        with self.lock:
            self.store[key] = rec
            self.times.append(secs)
            self.done_now += 1
            if secs > self.per_page * 0.75:
                self.slow.append([key, round(secs, 1)])
            n_done = sum(1 for k in self.keys if k in self.store)
            if self.out:
                tmp = self.out + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.store, f, ensure_ascii=False, indent=1, default=str)
                os.replace(tmp, self.out)
            err = rec.get("error")
            self.last = f"{n_done}/{len(self.keys)} {key}"
            if not self.quiet or err:
                print(f"[{n_done:>3}/{len(self.keys)} w{slot + 1} {secs:5.1f}s] " + (f"ERROR {key}: {err}" if err else f"ok    {key}"),
                      flush=True)

    def register(self):
        os.makedirs(RUNS, exist_ok=True)
        rec = {"pid": os.getpid(), "label": self.label, "site": (self.site or {}).get("key"), "windows": self.n,
               "started": self.t0, "budget": self.budget, "total": len(self.keys),
               "done": sum(1 for k in self.keys if k in self.store),
               "errors": sum(1 for k in self.keys if "error" in self.store.get(k, {})), "last": getattr(self, "last", None),
               "windows_now": [f"w{s + 1}: {w.item.key if w.item else 'idle'}" + (f" ({w.note})" if w.note else "")
                               for s, w in sorted(self.wins.items())]}
        path = os.path.join(RUNS, f"{os.getpid()}.json")
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def unregister(self):
        try:
            os.remove(os.path.join(RUNS, f"{os.getpid()}.json"))
        except OSError:
            pass


def _worker(slot, run):
    from playwright.sync_api import sync_playwright
    win = None
    try:
        with sync_playwright() as pw:
            while True:
                item = run.next()
                if item is None:
                    break
                t = time.time()
                try:
                    if win is None or win.killed:
                        if win is not None:
                            win.ready.wait(40)  # the keeper is replacing this window's Chrome
                        check_stack(run.machine)
                        win = run.wins[slot] = _Window(pw, slot, run)
                    p = win.tab
                    p._reset()
                    p.item, win.note = item.raw, ""
                    win.item, win.deadline = item, time.time() + run.per_page
                    if p.width != item.width:
                        p.emulate(item.width)
                    p.goto(item.target)
                    v = run.each(p, item.raw)
                    rec = {"v": v, "status": p.status}
                except StackDown as e:
                    run.fatal = str(e)
                    run.q.put(item)
                    break
                except LoadError as e:
                    rec = {"error": str(e), "kind": "load", "status": e.status}
                except BaseException as e:
                    if win is not None and win.killed:
                        rec = {"error": f"PAGE TIMEOUT: over {run.per_page}s (per_page); its window was closed and reopened"
                                        + (f"; last note: {win.note}" if win.note else ""), "kind": "timeout"}
                    elif win is None:
                        run.fatal = "CHROME DID NOT START: " + str(e).splitlines()[0][:300]
                        run.q.put(item)
                        break
                    else:
                        rec = {"error": f"{type(e).__name__}: {(str(e).strip().splitlines() or [''])[0][:400]}", "kind": "each",
                               "trace": traceback.format_exc()[-1500:]}
                        if not isinstance(e, Exception):
                            raise
                finally:
                    if win is not None:
                        win.item = win.deadline = None
                secs = time.time() - t
                rec["secs"] = round(secs, 2)
                run.save(item.key, rec, slot, secs)
            if win is not None:
                win.close()
    except BaseException as e:  # the Playwright driver itself failed
        if not run.fatal and not run.stopping:
            run.fatal = f"window {slot + 1} crashed: {type(e).__name__}: {str(e)[:300]}"
    finally:
        run.wins.pop(slot, None)


def run(site, items, each=None, out=None, windows=3, width=1440, height=1400, budget=240, per_page=60, label=None,
        dev=False, init_js=None, wait="load", load_timeout=30, fresh=True, quiet=False, fresh_out=False):
    """Run each(p, item) for every item, in `windows` Chrome windows at the same time. See the module docstring.
    Returns the summary (summary["results"] holds every saved item); raises PagesFailed if any item failed, Busy when
    the machine-wide window cap is reached, StackDown when Chrome may not start."""
    cfg = machine()
    check_stack(cfg)
    site = site_cfg(site) if isinstance(site, str) else site
    its, seen = [], set()
    for raw in items:
        it = _Item(raw, width)
        if it.key not in seen:
            seen.add(it.key)
            its.append(it)
    store = {}
    if out:
        out = os.path.abspath(out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            store = json.load(open(out, encoding="utf-8"))
    if fresh_out:
        store = {}
    todo = [it for it in its if not (it.key in store and "error" not in store[it.key])]
    if its and not todo and not quiet:
        age = round(time.time() - os.path.getmtime(out)) if out and os.path.exists(out) else 0
        print(f"NOTHING TO DO: all {len(its)} items are already saved in {out} (written {age}s ago). Read that file. "
              f"If it belongs to another task, use another OUT, or pass --fresh to run these items again.", flush=True)
    n = min(max(1, int(windows)), MAX_WINDOWS, max(1, len(todo)))
    label = label or os.path.basename(sys.argv[0] or "live")
    got = {"slots": [], "opened": 0, "reused": 0}
    if todo:
        got = _keeper("/acquire", {"pid": os.getpid(), "n": n, "cap": CAP, "label": label})
        if got.get("error"):
            raise StackDown("CHROME DID NOT START: " + got["error"])
        if got.get("busy"):
            raise Busy(f"BUSY: {got['held']} live windows are working (cap {CAP}). " + " | ".join(_run_line(r) for r in live_runs()))
        n = len(got["slots"])
    r = _Run(site=site, each=each or (lambda p, item: {"title": p.title(), "url": p.url}), out=out, n=n, width=width,
             height=height, budget=budget, per_page=per_page, label=label,
             dev=dev, init_js=init_js, wait=wait, load_timeout=load_timeout, fresh=fresh, machine=cfg, store=store,
             keys=[it.key for it in its], quiet=quiet, slots=dict(enumerate(got["slots"])))
    for it in todo:
        r.q.put(it)
    if not quiet:
        ws = sorted({it.width for it in its})
        print(f"live: {len(todo)} of {len(its)} items to do in {n} windows at {'/'.join(map(str, ws))}px"
              + (f" (opened {got['opened']}, reused {got['reused']})" if todo else "")
              + (f" (asked for {windows}; cap {CAP})" if n < min(int(windows), len(todo) or 1) else "")
              + f", budget {budget}s, {per_page}s per page", flush=True)
    threads = [threading.Thread(target=_worker, args=(i, r), daemon=True) for i in range(n)] if todo else []
    r.register()
    hard = budget + per_page + 20
    try:
        for t in threads:
            t.start()
        tick = 0
        while any(t.is_alive() for t in threads):
            time.sleep(0.25)
            tick += 1
            now = time.time()
            for w in list(r.wins.values()):
                if w.deadline and now > w.deadline and not w.killed:
                    w.kill()
            if os.path.exists(STOP) and not r.fatal:
                r.fatal = "WATCHDOG STOPPED CHROME: " + open(STOP).read().strip()
            if now - r.t0 > hard:
                r.fatal = r.fatal or f"HARD STOP: still running {hard}s after the start (a script step never returned)"
                break
            if tick % 4 == 0:
                r.register()
    except KeyboardInterrupt:
        r.fatal = "interrupted"
        raise
    finally:
        r.stopping = r.stopping or "end"
        for t in threads:
            t.join(3)
        try:
            if todo:
                _keeper("/release", {"pid": os.getpid()}, timeout=10)
        except Exception:
            pass
        r.unregister()
    left = [k for k in r.keys if k not in r.store]
    errors = {k: r.store[k]["error"] for k in r.keys if "error" in r.store.get(k, {})}
    summary = {"done_now": r.done_now, "done_total": len(r.keys) - len(left), "total": len(r.keys), "remaining": len(left),
               "errors": errors, "slow": r.slow, "windows": n, "wall_s": round(time.time() - r.t0, 1),
               "avg_page_s": round(sum(r.times) / len(r.times), 2) if r.times else None, "out": out,
               "windows_opened": got["opened"], "windows_reused": got["reused"],
               "stopped": r.fatal or r.stopping}
    if not quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    summary["results"] = r.store
    if r.fatal and ("WATCHDOG" in r.fatal or "CHROME DID NOT START" in r.fatal):
        raise StackDown(r.fatal)
    if errors:
        raise PagesFailed(summary)
    return summary


# ---- command line ----

def _unmsys(a):
    """Git Bash rewrites an argument like /pricing into C:/Program Files/Git/pricing before Python sees it; undo that."""
    if not os.environ.get("MSYSTEM") or not re.match(r"^[A-Za-z]:[/\\]", a):
        return a
    a2 = a.replace("\\", "/")
    root = re.sub(r"/(usr/|mingw64/)?bin/?$", "", (os.environ.get("EXEPATH") or "").replace("\\", "/"))
    for r in filter(None, [root, "C:/Program Files/Git"]):
        if a2.lower().startswith(r.lower() + "/"):
            return a2[len(r):]  # "/" itself arrives as the Git folder plus "/"
    return a


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s.strip("/")) or "home"


def main(argv):
    args = list(argv)

    def opt(name, default=None):
        if name in args:
            i = args.index(name)
            v = args[i + 1]
            del args[i:i + 2]
            return v
        return default

    def flag(name):
        if name in args:
            args.remove(name)
            return True
        return False

    if flag("--status"):
        runs = live_runs()
        print("\n".join(_run_line(r) + "".join("\n    " + w for w in r.get("windows_now") or []) for r in runs)
              or "no live runs")
        k = _keeper("/status")
        sl = k.get("slots") or []
        idle = [x["idle_s"] for x in sl if x["holder"] is None]
        print(f"live windows: {len(sl)} open, {len(sl) - len(idle)} working (cap {CAP})"
              + (f"; idle for {min(idle)}-{max(idle)}s, kept {k.get('keep_s')}s for the next run (live.py --close closes them)" if idle else ""))
        return 0
    if flag("--keeper"):
        return keeper_main()
    if flag("--close"):
        stopped = stop_runs()
        k = _keeper("/close")
        print(f"closed {k.get('closed', 0)} live window(s)" + (f", stopped {len(stopped)} run(s)" if stopped else ""))
        return 0
    if "--stop" in args:
        i = args.index("--stop")
        pid = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        hit = stop_runs(pid)
        print(f"stopped {len(hit)} live run(s): {hit}; their windows stay open (live.py --close closes them)" if hit else "no live run to stop")
        return 0
    cli = {"site": opt("--site"), "windows": opt("--windows"), "width": opt("--width"), "height": opt("--height"),
           "budget": opt("--budget"), "per_page": opt("--per-page"), "out": opt("--out"), "label": opt("--label"),
           "wait": opt("--wait")}
    widths, js_file, shots, items_file = opt("--widths"), opt("--js"), opt("--shots"), opt("--items")
    dev, fresh_out = flag("--dev"), flag("--fresh")
    script = args.pop(0) if args and args[0].endswith(".py") else None
    ns = {"__name__": "__live__", "run": run, "LoadError": LoadError, "PagesFailed": PagesFailed, "HERE": HERE, "json": json}
    code = None
    if script:
        ns["__file__"] = os.path.abspath(script)
        code = open(script, encoding="utf-8").read()
    elif not js_file and not shots and not args and not items_file and not sys.stdin.isatty():
        # a script on stdin, only when nothing else says what to do: never block on stdin when pages were given
        script, code = "<stdin>", sys.stdin.read()
    if code is not None and code.strip():
        exec(compile(code, script, "exec"), ns)
    each = ns.get("each")
    items = [_unmsys(x) for x in args] or (json.load(open(items_file, encoding="utf-8")) if items_file else None) or ns.get("ITEMS")
    if not items:
        if script and not each:
            return 0  # the script called run() itself
        print("NO ITEMS: pass paths or URLs, --items FILE.json, or define ITEMS in the script")
        return 1
    if script and not each:
        print(f"{script} must define each(p, item)")
        return 1
    if widths:
        ws = [int(x) for x in widths.split(",")]
        items = [dict(it if isinstance(it, dict) else {"path": it}, width=w) for it in items for w in ws]
    js = open(js_file, encoding="utf-8").read() if js_file else None
    if not each:
        def each(p, item):
            v = p.evaluate(js, item) if js else {"title": p.title(), "url": p.url}
            if shots:
                p.load_all()
                name = _safe(str(item.get("key") or item.get("path") or item.get("url")) if isinstance(item, dict) else str(item))
                shot = p.snap(os.path.join(shots, f"{name}@{p.width}.jpg"), full=True, quality=80)
                v = dict(v, shot=shot) if isinstance(v, dict) else {"v": v, "shot": shot}
            return v
    k = lambda name, default, cast=int: cast(cli[name]) if cli[name] is not None else ns.get(name.upper(), default)
    try:
        s = run(cli["site"] or ns.get("SITE"), items, each, out=cli["out"] or ns.get("OUT"), windows=k("windows", 3),
                width=k("width", 1440), height=k("height", 1400), budget=k("budget", 240), per_page=k("per_page", 60),
                label=cli["label"] or ns.get("LABEL") or os.path.basename(script or js_file or "live"),
                dev=dev or bool(ns.get("DEV")), init_js=ns.get("INIT_JS"), wait=k("wait", "load", str),
                load_timeout=int(ns.get("LOAD_TIMEOUT", 30)), fresh=bool(ns.get("FRESH", True)), fresh_out=fresh_out)
        code_ = 4 if s["remaining"] else 0
    except PagesFailed as e:
        s, code_ = e.summary, 1
        print(f"FAILED ITEMS ({len(s['errors'])}):" + "".join(f"\n  {key}: {msg}" for key, msg in s["errors"].items()))
    except Busy as e:
        print(e)
        return 5
    except StackDown as e:
        print("STACK DOWN: " + str(e))
        return 3
    if callable(ns.get("done")):
        ns["done"](s["results"], {x: y for x, y in s.items() if x != "results"})
    if s["remaining"]:
        print(f"REMAINING {s['remaining']}: run the same command again to continue")
        code_ = code_ or 4
    return code_


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("interrupted: windows closed")
        sys.exit(130)
