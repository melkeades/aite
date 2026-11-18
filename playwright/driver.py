"""Attach to the automation Chrome on :9222 and execute POSTed Python snippets (globals: page, context, browser).
If the watchdog wrote STOP or Chrome is gone, every request fails with stack_down=True (x.py exits 3) instead of
running the snippet. Once Chrome is back and STOP is removed, the next request reconnects and re-blocks video.

Guardrails (see README "Guardrails"):
- Every snippet has a time budget (X-Budget header, default 90 s, max 300). The HTTP reply always comes back by
  budget + 10 s. A snippet still running then is reported as over budget (x.py exits 4) and keeps the driver busy.
- One snippet at a time, never queued: a request that arrives while another runs is refused at once as BUSY
  (x.py exits 5) with what is running, for how long, and its last progress. POST /stop (x.py --stop) asks the running
  snippet to stop: its next check() raises.
- wf.py's Designer operations (ops.py: sync, set, qa, read) are snippets too. They work in Designer windows that stay
  open between commands (bridge.py _pool); the main loop closes the ones left idle.
- Live pages are not the driver's business: live.py opens its own Chromes and runs beside everything here.
- GET /status shows the running snippet (label, elapsed, budget, last progress), the kept Designer windows and the
  last finished snippet.
- Snippet helpers: progress(msg), check() (raises Budget when over budget), run_items(...) for batch work that
  saves after every item and stops before the budget runs out, evaluate_t(...) for a JS call with a hard timeout.
  Webflow helpers (js, until, switch_page, open_canvas, select, clip, paste_at, open_site, ...) come from bridge.py,
  loaded before each snippet and reloaded when the file changes.
- X-Site (x.py --site) names the Webflow site the snippet works on; the helpers resolve their tab from it. It is set
  for that snippet only, so two sessions sharing the driver never inherit each other's site.

Security: every request needs the X-Token header from the token file (a new random token on each start, readable only
by local processes, not by web pages). Requests with an Origin header (browsers) or a Host other than localhost (DNS
rebinding) are refused with 403.

Playwright's sync API must stay on the thread that started it, so snippets (and the Designer job service) run on the
main thread; the HTTP server runs on worker threads and only hands snippets over and waits for them."""
import json, os, sys, time, traceback, io, queue, threading, urllib.request, urllib.parse, secrets, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright

MEDIA = ["*.mp4*", "*.webm*", "*.mov*", "*.m3u8*", "*.ogv*"]
HERE = os.path.dirname(os.path.abspath(__file__))
# Overridable so a second instance can be tested without touching the real one.
STOP = os.environ.get("DRIVER_STOP", os.path.join(HERE, "STOP"))
PORT = int(os.environ.get("DRIVER_PORT", 8765))
CDP = "http://127.0.0.1:" + os.environ.get("CDP_PORT", "9222")
TOKEN_FILE = os.environ.get("DRIVER_TOKEN_FILE", os.path.join(HERE, f".driver-token-{PORT}"))
TOKEN = secrets.token_urlsafe(32)
HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
DEFAULT_BUDGET, MAX_BUDGET, REPLY_GRACE, MAX_BODY = 90, 300, 10, 5_000_000

pw = sync_playwright().start()
blocked = []
jobs = queue.Queue()
admit = threading.Lock()
state = {"busy": None, "queued": False, "last": None}
OWNED = set()  # CDP target ids of the kept session windows; shared-tab helpers skip them
_tl = threading.local()  # the running snippet's stdout buffer (out), on the main thread only


class _ThreadStdout:
    """print() inside a snippet goes to that snippet's buffer; other threads' prints are unaffected."""
    def __init__(self, base):
        self.base = base

    def write(self, s):
        return (getattr(_tl, "out", None) or self.base).write(s)

    def flush(self):
        (getattr(_tl, "out", None) or self.base).flush()


sys.stdout = _ThreadStdout(sys.stdout)


class Budget(Exception):
    """Raised by check() when the running snippet has used up its budget."""


class Stopped(Budget):
    """Raised by check() after x.py --stop. A Budget, so helpers that let Budget through let this through too."""


def cur():
    """The state of the running snippet."""
    return state["busy"]


def progress(msg):
    """Record what the snippet is doing; shown by GET /status and in BUSY / over-budget replies."""
    st = cur()
    if st:
        st["progress"], st["progress_at"] = str(msg)[:300], time.time()


def check():
    """Raise Budget if the running snippet is over its budget. Call it between steps of long work."""
    st = cur()
    if st and st.get("stop"):
        raise Stopped(f"stopped by x.py --stop; last progress: {st.get('progress')}")
    if st and time.time() - st["start"] > st["budget"]:
        raise Budget(f"budget {st['budget']}s used up; last progress: {st.get('progress')}")


def run_items(items, fn, out, key=lambda it: it, per_item=20):
    """Run fn(item) for each item not already in the JSON file `out`, saving after every item.
    Stops before the budget runs out (call again for the next batch). Items slower than per_item seconds are
    listed under "slow" so they get reported instead of silently waited on. Returns a summary dict."""
    st = cur()
    store = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    keys = [str(key(it)) for it in items]
    todo = [(k, it) for k, it in zip(keys, items) if not (k in store and not (isinstance(store[k], dict) and "error" in store[k]))]
    times, slow, stopped = [], [], None
    margin = min(3, 0.1 * st["budget"])
    for k, it in todo:
        # predict from measured items; the first item always runs if any budget is left, so every call progresses
        est = sum(times) / len(times) if times else 0
        if time.time() - st["start"] + est > st["budget"] - margin:
            stopped = "budget"
            break
        t0 = time.time()
        try:
            v = fn(it)
        except Budget:
            raise
        except Exception as e:
            v = {"error": str(e)[:300]}
        dt = time.time() - t0
        times.append(dt)
        if isinstance(v, dict):
            v["_secs"] = round(dt, 2)
        store[k] = v
        if dt > per_item:
            slow.append([k, round(dt, 1)])
        json.dump(store, open(out, "w", encoding="utf-8"), default=str)
        progress(f"{sum(1 for x in keys if x in store)}/{len(keys)} done, last {k} in {dt:.1f}s")
    left = sum(1 for x in keys if x not in store)
    return {"done_now": len(times), "done_total": len(keys) - left, "remaining": left, "stopped": stopped,
            "avg_s": round(sum(times) / len(times), 2) if times else None, "slow": slow,
            "errors": [x for x in keys if isinstance(store.get(x), dict) and "error" in store[x]]}


def evaluate_t(target, js, arg=None, ms=20000):
    """target.evaluate(js, arg), but rejected with 'ITEM TIMEOUT' after ms. js must be a function expression."""
    wrapped = (f"async (a) => Promise.race([({js})(a), new Promise((_, r) => "
               f"setTimeout(() => r(new Error('ITEM TIMEOUT {ms}ms')), {ms}))])")
    return target.evaluate(wrapped, arg)


ACTIVITY = os.path.join(HERE, "activity.log")


def activity(msg):
    """One line per thing that opens, reuses or closes windows (activity.log, shared with live.py): what a task really
    did on screen, for the user and for whoever debugs it afterwards."""
    try:
        with open(ACTIVITY, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


JOBWIN = {}    # kept Designer windows per site (bridge.py _pool); lives here so a bridge.py reload keeps them
env = {"json": json, "progress": progress, "check": check, "run_items": run_items, "evaluate_t": evaluate_t,
       "Budget": Budget, "HERE": HERE, "MEDIA": MEDIA, "OWNED": OWNED, "browser": None, "SITE": None,
       "JOBWIN": JOBWIN, "activity": activity, "cur": cur}
HELPERS = [os.path.join(HERE, "bridge.py"), os.path.join(HERE, "ops.py"), os.path.join(HERE, "store.py")]


def load_helpers():
    """Load bridge.py and then ops.py into the snippet globals; reload them whenever one of them (or store.py, which
    ops.py imports) changes, so edits need no restart."""
    m = tuple(os.path.getmtime(f) if os.path.exists(f) else None for f in HELPERS)
    if m != env.get("_helpers_mtime"):
        for f in HELPERS[:2]:
            if os.path.exists(f):
                exec(compile(open(f, encoding="utf-8").read(), f, "exec"), env)
        env["_helpers_mtime"] = m


def set_media_blocked(p, on):
    if (p in blocked) == on:
        return
    s = env["context"].new_cdp_session(p)
    s.send("Network.enable")
    s.send("Network.setBlockedURLs", {"urls": MEDIA if on else []})
    blocked.append(p) if on else blocked.remove(p)


def block_media(designer_tab=None):
    # Block video only in Webflow Designer tabs (the canvas plays every video on the page); other tabs, e.g. a
    # staging page, play video normally. Done in Chrome itself (no context.route: sync Playwright only answers
    # routed requests while a snippet is running, so an idle driver would stall every request in every tab).
    # designer_tab: a tab about to be navigated to the Designer, so it is blocked before the first request.
    for p in env["context"].pages:
        set_media_blocked(p, p is designer_tab or DESIGN in p.url)


DESIGN = ".design.webflow.com"
env["block_media"] = block_media  # helpers call it after opening a tab, before navigating it


def chrome_ws():
    """The running Chrome's browser websocket URL; it changes on every Chrome restart."""
    return json.loads(urllib.request.urlopen(CDP + "/json/version", timeout=3).read())[
        "webSocketDebuggerUrl"]


def connect():
    env["ws"] = chrome_ws()
    browser = pw.chromium.connect_over_cdp(CDP, timeout=10000)
    context = browser.contexts[0]
    page = next((p for p in context.pages if not p.is_closed()), None) or context.new_page()
    page.set_viewport_size({"width": 1600, "height": 1000})
    env.update(browser=browser, context=context, page=page)
    blocked.clear()
    block_media()


def stack_down():
    """None if Chrome is usable (reconnecting if needed), else the reason it is not. Main thread only."""
    if os.path.exists(STOP):
        return "WATCHDOG STOPPED CHROME: " + open(STOP).read().strip()
    try:
        ws = chrome_ws()
    except Exception:
        env["browser"] = None
        return f"CHROME DOWN: nothing listening on {CDP}"
    # is_connected() can still be True for a Chrome that was killed and restarted, so also compare the websocket
    if env["browser"] is not None and env["browser"].is_connected() and env.get("ws") == ws:
        return None
    try:
        connect()
        print("reconnected", flush=True)
        return None
    except Exception as e:
        return f"CHROME RECONNECT FAILED: {e}"


def busy_text():
    st = state["busy"]
    if not st:
        return "a snippet is about to start"
    return _describe(st)


def _describe(st):
    now = time.time()
    p = f"; last progress {now - st['progress_at']:.0f}s ago: {st['progress']}" if st.get("progress") else "; no progress reported"
    return f"'{st['label']}' running for {now - st['start']:.0f}s (budget {st['budget']}s){p}"


def run_code(job, env_, st):
    """Execute a snippet in env_ on this thread. Returns the reply body (without secs)."""
    out = io.StringIO()
    env_["result"] = None
    _tl.out = out
    try:
        t_exec = time.time()
        exec(job["code"], env_)
        body = {"ok": True, "stdout": out.getvalue(), "result": env_.get("result")}
        ran = time.time() - t_exec
        if ran > job["budget"]:
            # finished, but late: still an overrun (snippet never called check()); result is kept
            body.update(over_budget=True, error=f"OVER BUDGET: finished in {ran:.1f}s, budget {job['budget']}s "
                                                f"(snippet doesn't call check() often enough)")
    except Stopped as e:
        body = {"ok": False, "stopped": True, "stdout": out.getvalue(), "result": env_.get("result"),
                "error": f"STOPPED: {e}"}
    except Budget as e:
        body = {"ok": False, "over_budget": True, "stdout": out.getvalue(), "result": env_.get("result"),
                "error": f"OVER BUDGET: {e}"}
    except Exception:
        body = {"ok": False, "stdout": out.getvalue(), "error": traceback.format_exc()[-3000:]}
    finally:
        _tl.out = None
    return body


def execute(job):
    """Run one snippet (main thread)."""
    st = state["busy"] = {"label": job["label"], "start": time.time(), "budget": job["budget"]}
    activity(f"driver: snippet '{job['label']}' started" + (f" [{job['site']}]" if job.get("site") else ""))
    state["queued"] = False
    env["SITE"] = job.get("site")  # per snippet (X-Site / x.py --site), never carried over to the next one
    t0 = time.time()
    down = stack_down()
    if down:
        body = {"ok": False, "stack_down": True, "stdout": "", "error": down}
    else:
        try:
            block_media()
            load_helpers()
            body = run_code(job, env, st)
        except Exception:
            body = {"ok": False, "stdout": "", "error": traceback.format_exc()[-3000:]}
        tb = body.get("error") or ""
        if not body["ok"] and ("has been closed" in tb or "disconnected" in tb.lower()):
            env["browser"] = None
            down = stack_down()
            if down:
                body.update(stack_down=True, error=down + "\n" + tb)
    finish(job, body, t0)
    state["busy"] = None
    job["done"].set()


def finish(job, body, t0):
    secs = round(time.time() - t0, 1)
    body["secs"] = secs
    state["last"] = {"label": job["label"], "secs": secs, "ok": body["ok"], "ended": time.strftime("%H:%M:%S"),
                     "late": secs > job["budget"] + REPLY_GRACE}
    job["body"] = body


def service_jobs():
    """Between snippets: close kept Designer windows that have been idle too long, or all idle ones after /close."""
    close_all = state.pop("close_windows", False)
    if not JOBWIN or not (close_all or time.time() - state.get("idle_checked", 0) > 5):
        return
    state["idle_checked"] = time.time()
    try:
        if stack_down():
            return
        load_helpers()
        env["job_windows_idle"](close_all)
    except Exception as e:
        print("window service failed:", str(e)[:300], flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, code=200):
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def denied(self):
        """Why this request is refused, or None."""
        if "Origin" in self.headers:
            return "request comes from a browser (Origin header)"
        if self.headers.get("Host") not in HOSTS:
            return f"Host {self.headers.get('Host')!r} is not localhost"
        if not hmac.compare_digest(self.headers.get("X-Token", "").encode(), TOKEN.encode()):
            return "missing or wrong X-Token"
        return None

    def refuse(self, why):
        self.reply({"ok": False, "forbidden": True, "stdout": "", "error": "FORBIDDEN: " + why}, 403)

    def do_GET(self):
        why = self.denied()
        if why:
            return self.refuse(why)
        st = dict(state["busy"]) if state["busy"] else None
        if st:
            st["elapsed"] = round(time.time() - st["start"], 1)
        try:
            wins = env["job_windows_status"]() if "job_windows_status" in env else []
        except Exception:
            wins = []
        self.reply({"busy": st, "busy_text": busy_text() if st else None, "last": state["last"], "windows": wins})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 <= size <= MAX_BODY:
            self.close_connection = True
            return self.refuse(f"body size {size} outside 0..{MAX_BODY} bytes")
        # read the body before refusing, so the client gets the 403 instead of a reset connection
        raw = self.rfile.read(size).decode("utf-8", "replace")
        why = self.denied()
        if why:
            return self.refuse(why)
        u = urllib.parse.urlparse(self.path)
        if u.path == "/close":  # close the kept Designer job windows that are idle (the main loop does it)
            state["close_windows"] = True
            return self.reply({"ok": True})
        if u.path == "/stop":
            st = state["busy"]
            if st:
                st["stop"] = True
            return self.reply({"ok": bool(st), "error": None if st else "nothing is running",
                               "busy_text": busy_text() if st else None})
        try:
            budget = max(1, min(int(self.headers.get("X-Budget", DEFAULT_BUDGET)), MAX_BUDGET))
        except ValueError:
            budget = DEFAULT_BUDGET
        label = (self.headers.get("X-Label") or "snippet")[:120]
        site = (self.headers.get("X-Site") or "").strip()[:200] or None
        if (self.headers.get("X-Mode") or "").strip().lower() == "read":
            return self.reply({"ok": False, "stdout": "",
                               "error": "READ LANES ARE GONE: live pages run through live.py "
                                        "(python live.py --site KEY script.py); see README, Live pages"})
        job = {"code": raw, "budget": budget, "label": label, "site": site, "done": threading.Event()}
        with admit:
            if state["busy"] or state["queued"]:
                return self.reply({"ok": False, "busy": True, "stdout": "",
                                   "error": "BUSY: refused, not queued. " + busy_text()})
            state["queued"] = True
            jobs.put(job)
        if job["done"].wait(budget + REPLY_GRACE):
            return self.reply(job["body"])
        self.reply({"ok": False, "over_budget": True, "still_running": True, "stdout": "",
                    "error": f"OVER BUDGET, STILL RUNNING: '{label}'. {busy_text()}. New snippets are refused until it "
                             f"ends; x.py --stop asks it to stop at its next check()."})


with open(TOKEN_FILE, "w") as f:
    f.write(TOKEN)
print("driver ready", stack_down() or "connected", flush=True)
try:  # automation windows live on another virtual desktop when machine.json says so
    sys.path.insert(0, HERE)
    import desk
    desk.ensure()
except Exception as _e:
    print("desk not started:", _e, flush=True)
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever, daemon=True).start()
while True:
    try:
        _job = jobs.get(timeout=0.25)
    except queue.Empty:
        service_jobs()  # Designer job results, between snippets
        continue
    execute(_job)
    service_jobs()
