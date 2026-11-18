"""Sign-in handoff: a Chrome window on the user's desktop for them to sign in to a site, then the agent works in it.

    python hand.py open <url> [--key K]          start or reuse the Chrome for K (default: the URL's host), open the URL
    python hand.py status [--key K]              alive? its tabs, and a sign-in verdict
    python hand.py run [--key K] [--budget S] [--label T] snippet.py    (or the snippet on stdin)
    python hand.py stop [--key K]                ask the running snippet to stop (its next check() raises)
    python hand.py park | unpark [--key K]       move the window to the automation desktop / back to the user's
    python hand.py close [--key K] [--forget]    close that Chrome; --forget also deletes its profile (the saved sign-in)
    python hand.py list                          the open ones

One Chrome per key, each with its own persistent profile under hand-profiles/<key>/ (git-ignored: cookies and saved
sign-ins; never print, log or commit anything from it). It starts with the GPU flags from machine.json, muted, with
background throttling off (so it keeps painting while parked), and with the --aite-hand switch, which watchdog.py
counts as ours and desk.py leaves alone: the window opens on the desktop the user is on and may take the focus,
because the user types into it. `park` lists its pid in .hand.json, and desk.py then holds it on the automation
desktop like the other automation windows; `unpark` hands it back.

Snippets: Python, run in this process over CDP. Globals: page (the tab the work happens in), context, browser,
cdp (a CDP session on page), snap(path, clip=None, full=False, quality=None), progress(msg), check(), Budget, json,
KEY, URL. Set `result` to return something (printed as JSON). Never call page.bring_to_front() or activate a window.

Exit codes (like x.py):
  0 ok
  1 snippet error, stopped by `stop`, nothing open for that key, or a bad command
  3 stack down: STOP exists, the watchdog isn't running or doesn't know --aite-hand, machine.json missing
  4 over budget (a snippet still running at budget + 10 s is killed; Chrome and the page stay)
  5 busy: another snippet is running for that key; this one was refused, not queued

Windows idle for 30 minutes (no open/status/run/park by an agent; `hand_idle_s` in machine.json) are closed by a small
keeper process (`hand.py _keep`), which exits when nothing is open. The profile stays, so the next open is often
already signed in."""
import ctypes, json, os, re, shutil, subprocess, sys, threading, time, traceback, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".hand.json")
LOCK = os.path.join(HERE, ".hand.lock")
PROFILES = os.path.join(HERE, "hand-profiles")
MACHINE = os.path.join(HERE, "machine.json")
STOP = os.path.join(HERE, "STOP")
WATCHDOG = os.path.join(HERE, ".watchdog.json")
ACTIVITY = os.path.join(HERE, "activity.log")
MARK = "--aite-hand"
CHROME_ARGS = ["--mute-audio", "--no-first-run", "--no-default-browser-check", "--disable-accelerated-video-decode",
               "--autoplay-policy=user-gesture-required", "--disable-background-timer-throttling",
               "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
               "--disable-features=CalculateNativeWinOcclusion", "--hide-crash-restore-bubble", MARK]
DEFAULT_BUDGET, MAX_BUDGET, GRACE = 90, 300, 10
USER_DESKTOP = 1
sys.path.insert(0, HERE)


class StackDown(Exception):
    pass


class Fail(Exception):
    pass


class Budget(Exception):
    """Raised by check() when the snippet has used up its budget."""


class Stopped(Budget):
    """Raised by check() after `hand.py stop`."""


def activity(msg):
    try:
        with open(ACTIVITY, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} hand: {msg}\n")
    except OSError:
        pass


def machine():
    if not os.path.exists(MACHINE):
        raise StackDown(f"NO {MACHINE}: copy machine.example.json and set this machine's GPU flags (README 'Start')")
    return json.load(open(MACHINE, encoding="utf-8"))


def idle_s(cfg=None):
    if os.environ.get("HAND_IDLE_S"):
        return int(os.environ["HAND_IDLE_S"])
    try:
        return int((cfg or machine()).get("hand_idle_s", 1800))
    except Exception:
        return 1800


def alive(pid):
    if not pid:
        return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259


def check_stack(cfg):
    """Refuse to run without the watchdog guarding --aite-hand Chromes, as live.py does for its own."""
    if os.path.exists(STOP):
        raise StackDown("WATCHDOG STOPPED CHROME: " + open(STOP).read().strip())
    if not cfg.get("watchdog", True):
        return
    log = os.path.join(HERE, "gpu.log")
    if not os.path.exists(log) or time.time() - os.path.getmtime(log) > 20:
        raise StackDown("WATCHDOG NOT RUNNING (gpu.log is stale): start it with the README 'Start' command")
    try:
        w = json.load(open(WATCHDOG, encoding="utf-8"))
    except Exception:
        w = {}
    if not alive(w.get("pid")) or MARK not in (w.get("marks") or []):
        raise StackDown("WATCHDOG DOESN'T WATCH --aite-hand: the running watchdog.py is older than hand.py; "
                        "ask the user, then restart it with the README 'Start' command")


# ---- state (.hand.json), shared with desk.py, which reads "parked" ----

class _Lock:
    def __enter__(self):
        import msvcrt
        self.f = open(LOCK, "a+")
        end = time.time() + 10
        while True:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > end:
                    raise Fail("hand.py: .hand.lock held for 10 s by another hand.py")
                time.sleep(0.05)

    def __exit__(self, *a):
        import msvcrt
        try:
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


def load():
    try:
        s = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("keys", {})
    s.setdefault("parked", [])
    return s


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def update(fn):
    with _Lock():
        s = load()
        r = fn(s)
        save(s)
        return r


def prune(s):
    """Drop entries whose Chrome is gone. Returns the keys dropped."""
    gone = [k for k, e in s["keys"].items() if not alive(e.get("pid"))]
    for k in gone:
        pid = s["keys"].pop(k)["pid"]
        if pid in s["parked"]:
            s["parked"].remove(pid)
    return gone


def touch(key):
    def f(s):
        if key in s["keys"]:
            s["keys"][key]["last_used"] = time.time()
    update(f)


def key_of(url):
    host = (urllib.parse.urlparse(url).hostname or "site").lower()
    host = host[4:] if host.startswith("www.") else host
    return re.sub(r"[^a-z0-9.-]", "-", host) or "site"


def site_of(host):
    """The registrable part of a host, roughly: example.com for app.example.com, example.co.uk for a.example.co.uk."""
    host = (host or "").lower()
    if re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?|[^.]*", host):  # an IP address or a single-label host: all of it
        return host
    parts = host.split(".")
    if len(parts) > 2 and len(parts[-2]) <= 3 and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def pick(s, key):
    """The entry for key, or the only one open. Raises Fail with what is open otherwise."""
    prune(s)
    if key:
        if key not in s["keys"]:
            raise Fail(f"NOTHING OPEN for {key!r}" + (f"; open: {', '.join(s['keys'])}" if s["keys"] else ""))
        return key, s["keys"][key]
    if len(s["keys"]) == 1:
        return next(iter(s["keys"].items()))
    raise Fail("NOTHING OPEN" if not s["keys"] else f"SEVERAL OPEN, pass --key: {', '.join(s['keys'])}")


def entry(key):
    with _Lock():
        s = load()
        k, e = pick(s, key)
        save(s)
        return k, e


# ---- Chrome ----

def chrome_exe(cfg):
    if cfg.get("chrome"):
        return cfg["chrome"]
    import winreg
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            return winreg.QueryValue(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
        except OSError:
            pass
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def running_on(profile):
    """The browser process already running on this profile (one not in .hand.json, e.g. after the state was lost)."""
    import psutil
    want = os.path.normcase(profile)
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = p.info["cmdline"] or []
            if any(a.startswith("--type=") for a in cmd):
                continue
            for a in cmd:
                if a.startswith("--user-data-dir=") and os.path.normcase(a.split("=", 1)[1].strip('"')) == want:
                    return p.pid
        except Exception:
            pass
    return None


def read_port(profile, timeout=20, since=0):
    f, end = os.path.join(profile, "DevToolsActivePort"), time.time() + timeout
    while time.time() < end:
        try:
            if os.path.getmtime(f) >= since:
                return int(open(f).read().split()[0])
        except Exception:
            pass
        time.sleep(0.1)
    raise Fail("Chrome did not open its debugging port in %ds" % timeout)


def launch(key, url, cfg):
    profile = os.path.join(PROFILES, key)
    os.makedirs(profile, exist_ok=True)
    pid = running_on(profile)
    if pid:
        return pid, read_port(profile, 5), profile, False
    try:
        os.remove(os.path.join(profile, "DevToolsActivePort"))
    except OSError:
        pass
    args = [chrome_exe(cfg)] + CHROME_ARGS + list(cfg.get("chrome_args") or []) + [
        f"--user-data-dir={profile}", "--remote-debugging-port=0", "--window-size=1400,950", url]
    si = subprocess.STARTUPINFO()  # shown normally: this window is meant for the user's keyboard
    si.dwFlags, si.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1  # SW_SHOWNORMAL
    t0 = time.time() - 1
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives this command
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                startupinfo=si, creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                startupinfo=si, creationflags=flags)
    return proc.pid, read_port(profile, 20, t0), profile, True


def connect(port):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        return pw, pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
    except Exception as e:
        pw.stop()
        raise Fail(f"CANNOT CONNECT to the Chrome on :{port}: {str(e).splitlines()[0][:200]}")


def target_id(context, page):
    s = context.new_cdp_session(page)
    try:
        return s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        s.detach()


def normal_pages(context):
    return [p for p in context.pages if not p.is_closed()
            and not p.url.startswith(("devtools://", "chrome-extension://", "chrome://"))]


def work_page(browser, e):
    """The tab the work happens in: the one `open` used if it is still there, else the last normal tab."""
    context = browser.contexts[0]
    pages = normal_pages(context)
    if not pages:
        return context, context.new_page()
    for p in pages:
        try:
            if target_id(context, p) == e.get("target"):
                return context, p
        except Exception:
            pass
    return context, pages[-1]


def windows_of(pid, timeout=0):
    import desk
    end = time.time() + timeout
    while True:
        w = desk.chrome_windows({pid})
        if w or time.time() >= end:
            return w
        time.sleep(0.1)


def desktop_of(pid):
    """1-based virtual desktop the Chrome's first window shows on, or None. An owned window (parked: desk.py's anchor
    owns it) shows on its owner's desktop, while Windows keeps reporting the window's own earlier desktop for it."""
    import desk
    w = windows_of(pid)
    if not w:
        return None
    h = w[0]
    for _ in range(4):
        owner = ctypes.windll.user32.GetWindow(h, 4)  # GW_OWNER
        if not owner:
            break
        h = owner
    return desk.window_desktop(h)


# ---- the sign-in verdict ----

PROBE = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const inputs = [...document.querySelectorAll('input')].filter(vis);
  const label = i => [i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.autocomplete].join(' ');
  const pw = inputs.filter(i => i.type === 'password').length;
  const otp = inputs.filter(i => i.autocomplete === 'one-time-code'
                || /\b(otp|totp|mfa|2fa)\b|verification.?code|one.?time|security.?code/i.test(label(i))).length;
  const captcha = [...document.querySelectorAll('iframe')].some(f => /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha/i.test(f.src || ''))
                || !!document.querySelector('.g-recaptcha,.h-captcha,.cf-turnstile,#challenge-form,#cf-challenge-running');
  const texts = [...document.querySelectorAll('a,button,[role=button]')].filter(vis)
                .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim().toLowerCase()).filter(t => t && t.length < 40);
  return {url: location.href, title: document.title, pw, otp, captcha,
          sign_in: texts.some(t => /^(sign ?in|log ?in|login)$/.test(t)),
          sign_out: texts.some(t => /sign ?out|log ?out|logout/.test(t))};
}"""
LOGIN_PATH = re.compile(r"/(log-?in|sign-?in|signin|auth|authorize|sso|oauth2?|saml|session/new|users/sign_in|"
                        r"account/login|challenge|mfa|2fa|verify)(/|$|\?|\.)", re.I)
LOGIN_HOST = re.compile(r"^(accounts|login|auth|sso|signin|id|identity|idp)\.", re.I)


def verdict(info, site, cookies, baseline):
    """('SIGNED IN' | 'NOT YET' | 'UNKNOWN', why). info: PROBE merged over every frame of the work tab."""
    u = urllib.parse.urlparse(info.get("url") or "")
    host = (u.hostname or "").lower()
    if info.get("captcha"):
        return "NOT YET", "a CAPTCHA or bot check is showing: that one is the user's"
    if info.get("pw") or info.get("otp"):
        return "NOT YET", "a " + ("password" if info.get("pw") else "one-time code") + " field is showing"
    if LOGIN_PATH.search(u.path + ("?" + u.query if u.query else "")) or LOGIN_HOST.match(host):
        return "NOT YET", f"on a sign-in page: {host}{u.path}"
    if site_of(host) != site:
        return "UNKNOWN", f"on another site: {host}"
    grew = cookies > baseline
    if info.get("sign_out"):
        return "SIGNED IN", "back on the site, and it offers a sign-out"
    if info.get("sign_in"):
        return "UNKNOWN", "back on the site, but it offers a Sign in link and no sign-out"
    if grew:
        return "SIGNED IN", f"back on the site, nothing asks for a sign-in, and it set cookies ({baseline} -> {cookies})"
    return "SIGNED IN", "on the site and nothing asks for a sign-in (no new cookies since open)"


def probe(context, page, site):
    info = {"url": page.url, "title": "", "pw": 0, "otp": 0, "captcha": False, "sign_in": False, "sign_out": False}
    for i, f in enumerate(page.frames):
        try:
            r = f.evaluate(PROBE)
        except Exception:
            continue
        if i == 0:
            info.update(url=r["url"], title=r["title"])
        for k in ("pw", "otp"):
            info[k] += r[k]
        for k in ("captcha", "sign_in", "sign_out"):
            info[k] = info[k] or r[k]
    cookies = sum(1 for c in context.cookies() if site_of(c.get("domain", "").lstrip(".")) == site)
    return info, cookies


def report(key, e, context, page):
    info, cookies = probe(context, page, e["site"])
    v, why = verdict(info, e["site"], cookies, e.get("baseline", 0))
    print(f"VERDICT: {v} ({why})")
    print(f"tab: {info['url'][:200]}  \"{info['title'][:80]}\"")
    others = [p.url[:120] for p in normal_pages(context) if p is not page]
    if others:
        print("other tabs: " + " | ".join(others))
    d = desktop_of(e["pid"])
    where = "parked on the automation desktop" if e["pid"] in load()["parked"] else f"desktop {d}" if d else "no window"
    print(f"window: {where}; cookies for {e['site']}: {cookies} (at open: {e.get('baseline', 0)})")
    return v


# ---- commands ----

def ensure_keeper():
    def f(s):
        if alive(s.get("keeper")):
            return None
        log = open(os.path.join(HERE, "hand.out"), "a")
        p = subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__), "_keep"], cwd=HERE, stdout=log,
                             stderr=log, stdin=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200 | 0x08000000)
        s["keeper"] = p.pid
    update(f)


def cmd_open(url, key):
    if not re.match(r"^https?://", url):
        url = "https://" + url
    cfg = machine()
    check_stack(cfg)
    key = key or key_of(url)
    with _Lock():
        s = load()
        prune(s)
        e = s["keys"].get(key)
        was_parked = bool(e and e["pid"] in s["parked"])
        if was_parked:
            s["parked"].remove(e["pid"])
        save(s)
    if was_parked:
        print("unparked: the window is coming back to this desktop")
    if e:
        pw, browser = connect(e["port"])
        try:
            context, page = work_page(browser, e)
            try:
                page.goto(url, wait_until="load", timeout=30000)
            except Exception as ex:
                print("load not finished in 30 s:", str(ex).splitlines()[0][:160])
            e["url"], e["site"], e["target"] = url, site_of(urllib.parse.urlparse(url).hostname), target_id(context, page)
            e["baseline"] = probe(context, page, e["site"])[1]
            e["last_used"] = time.time()
            update(lambda s: s["keys"].__setitem__(key, e))
            activity(f"open (reused) {key}")
            print(f"REUSED the Chrome for {key} (pid {e['pid']}); navigated its tab to {url}")
            report(key, e, context, page)
        finally:
            pw.stop()
        ensure_keeper()
        return 0
    pid, port, profile, started = launch(key, url, cfg)
    activity(f"open {key} ({'started' if started else 'adopted'} pid {pid})")
    e = {"pid": pid, "port": port, "url": url, "site": site_of(urllib.parse.urlparse(url).hostname), "opened": time.time(),
         "last_used": time.time()}
    pw, browser = connect(port)
    try:
        context = browser.contexts[0]
        end, page = time.time() + 10, None
        while time.time() < end and page is None:
            page = next((p for p in normal_pages(context) if p.url not in ("about:blank", "")), None)
            if page is None:
                time.sleep(0.2)
        if page is None:
            page = normal_pages(context)[0] if normal_pages(context) else context.new_page()
            page.goto(url, wait_until="load", timeout=30000)
        try:
            page.wait_for_load_state("load", timeout=30000)
        except Exception:
            print("load not finished in 30 s; the verdict below may change")
        e["target"] = target_id(context, page)
        e["baseline"] = probe(context, page, e["site"])[1]
        update(lambda s: s["keys"].__setitem__(key, e))
        print(f"{'OPENED' if started else 'ADOPTED'} {url} in the Chrome for {key} (pid {pid}, CDP :{port})")
        if windows_of(pid, 5):
            d = desktop_of(pid)
            if d and d != USER_DESKTOP:
                print(f"NOTE: the window is on desktop {d}, not {USER_DESKTOP}: it opened where the user was. "
                      f"They can switch there with Ctrl+Win+Left/Right.")
        report(key, e, context, page)
    finally:
        pw.stop()
    ensure_keeper()
    return 0


def cmd_status(key):
    key, e = entry(key)
    pw, browser = connect(e["port"])
    try:
        context, page = work_page(browser, e)
        print(f"{key}: Chrome pid {e['pid']}, CDP :{e['port']}, opened {time.strftime('%H:%M', time.localtime(e['opened']))}")
        report(key, e, context, page)
    finally:
        pw.stop()
    touch(key)
    return 0


def run_file(key):
    return os.path.join(HERE, f".hand-run-{key}.json")


def stop_file(key):
    return os.path.join(HERE, f".hand-stop-{key}")


def cmd_run(key, code, budget, label):
    check_stack(machine())
    key, e = entry(key)
    budget = max(1, min(int(budget), MAX_BUDGET))
    rf = run_file(key)
    with _Lock():
        try:
            r = json.load(open(rf, encoding="utf-8"))
        except Exception:
            r = None
        if r and alive(r.get("pid")) and r.get("pid") != os.getpid():
            print(f"BUSY: refused, not queued. '{r['label']}' running for {time.time() - r['start']:.0f}s "
                  f"(budget {r['budget']}s); last progress: {r.get('progress')}")
            return 5
        st = {"pid": os.getpid(), "label": label, "start": time.time(), "budget": budget, "progress": None}
        json.dump(st, open(rf, "w", encoding="utf-8"))
    try:
        os.remove(stop_file(key))
    except OSError:
        pass

    def over():
        print(f"OVER BUDGET, KILLED: '{label}' still running at {budget + GRACE}s (budget {budget}s); last progress: "
              f"{st.get('progress')}. Chrome and its page are untouched.", flush=True)
        try:
            os.remove(rf)
        except OSError:
            pass
        os._exit(4)

    timer = threading.Timer(budget + GRACE, over)
    timer.daemon = True
    timer.start()
    activity(f"run '{label}' [{key}]")
    t0, pw = time.time(), None
    try:
        pw, browser = connect(e["port"])
        context, page = work_page(browser, e)
        cdp = context.new_cdp_session(page)

        def progress(msg):
            st["progress"] = str(msg)[:300]
            try:
                json.dump(st, open(rf, "w", encoding="utf-8"))
            except OSError:
                pass

        def check():
            if os.path.exists(stop_file(key)):
                raise Stopped(f"stopped by hand.py stop; last progress: {st.get('progress')}")
            if time.time() - t0 > budget:
                raise Budget(f"budget {budget}s used up; last progress: {st.get('progress')}")

        def snap(path, clip=None, full=False, quality=None):
            """CDP capture of the work tab (no Playwright screenshot: those can hang). clip in CSS px."""
            import base64
            fmt = "jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "webp" if path.lower().endswith(".webp") else "png"
            args = {"format": fmt}
            if quality and fmt != "png":
                args["quality"] = int(quality)
            if full and not clip:
                m = cdp.send("Page.getLayoutMetrics")["cssContentSize"]
                clip = {"x": 0, "y": 0, "width": m["width"], "height": m["height"]}
                args["captureBeyondViewport"] = True
            if clip:
                args["clip"] = dict({"scale": 1}, **clip)
            data = cdp.send("Page.captureScreenshot", args)["data"]
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            return path

        env = {"page": page, "context": context, "browser": browser, "cdp": cdp, "snap": snap, "progress": progress,
               "check": check, "Budget": Budget, "json": json, "KEY": key, "URL": e["url"], "HERE": HERE,
               "result": None, "__name__": "__snippet__"}
        exec(compile(code, label, "exec"), env)
        took = time.time() - t0
        if env.get("result") is not None:
            print("RESULT:", json.dumps(env["result"], ensure_ascii=False, default=str, indent=1))
        if took > budget:
            print(f"OVER BUDGET: finished in {took:.1f}s, budget {budget}s (the snippet doesn't call check() often enough)")
            return 4
        print(f"[{took:.1f}s of {budget}s]")
        return 0
    except Stopped as ex:
        print(f"STOPPED: {ex}")
        return 1
    except Budget as ex:
        print(f"OVER BUDGET: {ex}")
        return 4
    except Fail:
        raise
    except Exception:
        print(traceback.format_exc()[-3000:])
        return 1
    finally:
        timer.cancel()
        try:
            os.remove(rf)
        except OSError:
            pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        touch(key)


def cmd_stop(key):
    key, e = entry(key)
    try:
        r = json.load(open(run_file(key), encoding="utf-8"))
    except Exception:
        r = None
    if not r or not alive(r.get("pid")):
        print("nothing is running for", key)
        return 1
    open(stop_file(key), "w").close()
    print(f"asked '{r['label']}' to stop at its next check()")
    return 0


def _desk_ready():
    import desk
    s = desk.state() or desk.ensure()
    if not s:
        raise Fail("desk.py isn't running and couldn't start (machine.json needs \"desktop\", and that desktop must exist)")
    if not s.get("parks"):
        raise Fail("THE RUNNING desk.py IS OLDER than hand.py and can't park windows: ask the user, then "
                   "`python desk.py --stop` (the automation windows come back to this desktop for a moment) and run the "
                   "command again (it starts the new desk.py)")
    return s


def cmd_park(key, on):
    import desk
    key, e = entry(key)
    target = _desk_ready()["desktop"] if on else desk.wanted_desktop()

    def f(s):
        p = s["parked"]
        if on and e["pid"] not in p:
            p.append(e["pid"])
        if not on and e["pid"] in p:
            p.remove(e["pid"])
        if key in s["keys"]:
            s["keys"][key]["last_used"] = time.time()
    update(f)
    end, d = time.time() + 10, None  # desk.py's process scan takes ~2.5 s, a fresh daemon a few s more
    while time.time() < end:
        d = desktop_of(e["pid"])
        if (d == target) if on else (d is not None and d != target):
            break
        time.sleep(0.15)
    activity(f"{'park' if on else 'unpark'} {key}")
    if on:
        print(f"PARKED {key} on desktop {d} (Ctrl+Win+Right to watch)" if d == target
              else f"PARK ASKED, but after 10 s the window still shows on desktop {d}: check `python desk.py --status`")
    else:
        print(f"UNPARKED {key}: the window is on desktop {d}")
    return 0


def close_entry(key, e, forget=False):
    """Close one Chrome gracefully (CDP Browser.close), then by its pid if it lingers. Never touches other Chromes."""
    try:
        pw, browser = connect(e["port"])
        try:
            browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass
        finally:
            try:
                pw.stop()
            except Exception:
                pass
    except Fail:
        pass
    end = time.time() + 10
    while alive(e["pid"]) and time.time() < end:
        time.sleep(0.2)
    if alive(e["pid"]):
        subprocess.run(["taskkill", "/PID", str(e["pid"]), "/T", "/F"], capture_output=True)

    def f(s):
        s["keys"].pop(key, None)
        if e["pid"] in s["parked"]:
            s["parked"].remove(e["pid"])
    update(f)
    activity(f"close {key}" + (" (profile deleted)" if forget else ""))
    if forget:
        prof = os.path.join(PROFILES, key)
        for _ in range(20):
            shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(prof):
                break
            time.sleep(0.25)
        return not os.path.exists(prof)
    return True


def cmd_close(key, forget):
    try:
        key, e = entry(key)
    except Fail:
        if key and forget and os.path.isdir(os.path.join(PROFILES, key)):
            shutil.rmtree(os.path.join(PROFILES, key), ignore_errors=True)
            print(f"{key} wasn't open; profile deleted")
            return 0
        raise
    ok = close_entry(key, e, forget)
    print(f"CLOSED {key}" + ("; profile deleted (the saved sign-in is gone)" if forget and ok
                             else "; PROFILE NOT DELETED (files still locked)" if forget else "; profile kept"))
    return 0 if ok else 1


def cmd_list():
    s = update(lambda s: (prune(s), s)[1])
    if not s["keys"]:
        print("nothing open")
    for k, e in s["keys"].items():
        idle = time.time() - e.get("last_used", e["opened"])
        print(f"{k}: pid {e['pid']}, CDP :{e['port']}, {'parked' if e['pid'] in s['parked'] else 'on the user desktop'}, "
              f"idle {idle / 60:.0f} min, {e['url'][:100]}")
    return 0


def keeper():
    """Close windows idle for idle_s(); exit when nothing is open."""
    limit = idle_s()
    print(time.strftime("%H:%M:%S"), f"hand keeper up, idle limit {limit}s", flush=True)
    while True:
        time.sleep(min(15, max(1, limit / 4)))
        try:
            with _Lock():
                s = load()
                if s.get("keeper") != os.getpid():
                    return
                prune(s)
                if not s["keys"]:
                    s["keeper"] = None
                    save(s)
                    print(time.strftime("%H:%M:%S"), "nothing open; keeper exits", flush=True)
                    return
                save(s)
                stale = []
                for k, e in s["keys"].items():
                    try:
                        busy = alive(json.load(open(run_file(k), encoding="utf-8")).get("pid"))
                    except Exception:
                        busy = False
                    if not busy and time.time() - e.get("last_used", e["opened"]) > limit:
                        stale.append((k, e))
            for k, e in stale:
                print(time.strftime("%H:%M:%S"), f"closing {k}: idle over {limit}s", flush=True)
                close_entry(k, e)
        except Exception as ex:
            print(time.strftime("%H:%M:%S"), "keeper:", ex, flush=True)


def main(argv):
    def opt(name, default=None):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    def flag(name):
        if name in argv:
            argv.remove(name)
            return True
        return False

    key, budget, label = opt("--key"), opt("--budget", DEFAULT_BUDGET), opt("--label")
    forget = flag("--forget")
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "open" and rest:
            return cmd_open(rest[0], key)
        if cmd == "status":
            return cmd_status(key)
        if cmd == "run":
            src = rest[0] if rest and rest[0] != "-" else None
            code = open(src, encoding="utf-8").read() if src else sys.stdin.read()
            return cmd_run(key, code, budget, label or (os.path.basename(src) if src else "snippet"))
        if cmd == "stop":
            return cmd_stop(key)
        if cmd in ("park", "unpark"):
            return cmd_park(key, cmd == "park")
        if cmd == "close":
            return cmd_close(key, forget)
        if cmd == "list":
            return cmd_list()
        if cmd == "_keep":
            keeper()
            return 0
    except StackDown as e:
        print(e)
        return 3
    except Fail as e:
        print(e)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
