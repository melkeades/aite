"""hand.py end to end, against a fake site with a sign-in form on 127.0.0.1 (fake credentials, nothing leaves the PC).

    python tests/test_hand.py

Needs the watchdog running (README "Start") and opens one real Chrome window on the current desktop for about a
minute (key "hand-test", profile deleted at the end). The fake sign-in is done by a snippet, standing in for the user.
With a second virtual desktop and "desktop" in machine.json it also checks park/unpark; desk.py is stopped again
afterwards if this test started it. Every row must pass."""
import ctypes, http.server, json, os, re, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import desk, hand  # noqa: E402

KEY = "hand-test"
rows = []


def row(name, ok, detail=""):
    rows.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)


def H(*args, stdin=None, timeout=60):
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(HERE, "hand.py")] + list(args), input=stdin, capture_output=True,
                       text=True, encoding="utf-8", timeout=timeout, cwd=HERE)
    return p.returncode, p.stdout + p.stderr, time.time() - t0


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def page(self, body, code=200, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        signed = "sid=ok" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/app"):
            if not signed:
                return self.page("", 302, [("Location", "/login")])
            return self.page("<title>Dashboard</title><h1>Dashboard</h1><button>Sign out</button>")
        if self.path.startswith("/login"):
            return self.page("<title>Log in</title><form method=post action=/login><input name=email>"
                             "<input type=password name=password><button>Log in</button></form>")
        if self.path.startswith("/public"):
            return self.page("<title>Home</title><a href=/login>Sign in</a>")
        self.page("not found", 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.page("", 302, [("Set-Cookie", "sid=ok; Path=/; Max-Age=3600"), ("Location", "/app")])


def unit():
    v = hand.verdict
    base = {"url": "https://app.example.com/home", "pw": 0, "otp": 0, "captcha": False, "sign_in": False, "sign_out": False}
    row("verdict: password field -> NOT YET", v(dict(base, pw=1), "example.com", 3, 0)[0] == "NOT YET")
    row("verdict: code field -> NOT YET", v(dict(base, otp=1), "example.com", 3, 0)[0] == "NOT YET")
    row("verdict: CAPTCHA -> NOT YET", v(dict(base, captcha=True, sign_out=True), "example.com", 3, 0)[0] == "NOT YET")
    row("verdict: /login path -> NOT YET", v(dict(base, url="https://example.com/login?next=/"), "example.com", 0, 0)[0] == "NOT YET")
    row("verdict: accounts.* host -> NOT YET", v(dict(base, url="https://accounts.google.com/x"), "example.com", 0, 0)[0] == "NOT YET")
    row("verdict: another site -> UNKNOWN", v(dict(base, url="https://other.org/"), "example.com", 0, 0)[0] == "UNKNOWN")
    row("verdict: sign-out offered -> SIGNED IN", v(dict(base, sign_out=True), "example.com", 0, 0)[0] == "SIGNED IN")
    row("verdict: only a Sign in link -> UNKNOWN", v(dict(base, sign_in=True), "example.com", 5, 0)[0] == "UNKNOWN")
    row("verdict: new cookies -> SIGNED IN", v(base, "example.com", 4, 1)[0] == "SIGNED IN")
    row("site_of", hand.site_of("app.example.com") == "example.com" and hand.site_of("a.b.example.co.uk") == "example.co.uk"
        and hand.site_of("127.0.0.1") == "127.0.0.1" and hand.site_of("localhost") == "localhost")
    row("key_of", hand.key_of("https://www.Example.com/x") == "example.com")


def hand_pid():
    return hand.load()["keys"].get(KEY, {}).get("pid")


def end_to_end():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    H("close", "--key", KEY, "--forget")
    desk_was = desk.state()

    rc, out, _ = H("open", base + "/app", "--key", KEY)
    row("open: exit 0, redirected to the sign-in form -> NOT YET", rc == 0 and "VERDICT: NOT YET" in out, out)
    pid = hand_pid()
    row("open: window on desktop 1", "window: desktop 1" in out, out)
    rc, out, _ = H("status", "--key", KEY)
    row("status before sign-in: NOT YET", rc == 0 and "VERDICT: NOT YET" in out, out)

    fill = ("page.fill('input[name=email]', 'test@example.test'); page.fill('input[type=password]', 'fake-pass');"
            "page.click('button'); page.wait_for_url('**/app'); result = page.title()")
    rc, out, _ = H("run", "--key", KEY, "-", stdin=fill)
    row("run: fake sign-in snippet, RESULT printed", rc == 0 and '"Dashboard"' in out, out)
    rc, out, _ = H("status", "--key", KEY)
    row("status after sign-in: SIGNED IN", rc == 0 and "VERDICT: SIGNED IN" in out, out)

    rc, out, _ = H("run", "--key", KEY, "--budget", "2", "-", stdin="import time\nwhile True:\n    check(); time.sleep(0.1)")
    row("run: check() past the budget -> exit 4", rc == 4 and "OVER BUDGET" in out, out)
    rc, out, dt = H("run", "--key", KEY, "--budget", "1", "-", stdin="import time; time.sleep(40)")
    row("run: snippet ignoring check() killed at budget + 10 s -> exit 4", rc == 4 and "KILLED" in out and dt < 20, f"{dt:.1f}s {out}")
    rc, out, _ = H("run", "--key", KEY, "-", stdin="result = page.evaluate('1+1')")
    row("run: works again after a kill", rc == 0 and "RESULT: 2" in out, out)

    bg = subprocess.Popen([sys.executable, os.path.join(HERE, "hand.py"), "run", "--key", KEY, "--budget", "20",
                           "--label", "long", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, cwd=HERE)
    bg.stdin.write("import time\nwhile True:\n    check(); progress('waiting'); time.sleep(0.1)")
    bg.stdin.close()
    time.sleep(4)
    rc, out, _ = H("run", "--key", KEY, "-", stdin="result = 1")
    row("run while another runs -> exit 5 BUSY", rc == 5 and "BUSY" in out and "'long'" in out, out)
    rc, out, _ = H("stop", "--key", KEY)
    bg_out = bg.communicate(timeout=20)[0]
    row("stop: the running snippet ends with STOPPED, exit 1", rc == 0 and bg.returncode == 1 and "STOPPED" in bg_out, out + bg_out)

    wd = subprocess.run([sys.executable, os.path.join(HERE, "watchdog.py"), "--pids"], capture_output=True, text=True, cwd=HERE)
    row("watchdog counts the hand Chrome as ours", pid in json.loads(wd.stdout or "[]"), wd.stdout)

    if desk.wanted_desktop() and len(desk.desktop_ids()) >= desk.wanted_desktop():
        u32 = ctypes.windll.user32
        fg_before = desk.window_desktop(u32.GetForegroundWindow())
        rc, out, _ = H("park", "--key", KEY)
        time.sleep(0.5)
        d = hand.desktop_of(pid)
        row(f"park: window on desktop {desk.wanted_desktop()}", rc == 0 and "PARKED" in out and d == desk.wanted_desktop(), f"desktop {d}: {out}")
        fg_after = desk.window_desktop(u32.GetForegroundWindow())
        row("park: the user's screen stays on its desktop", fg_after in (fg_before, None), f"{fg_before} -> {fg_after}")
        rc, out, _ = H("run", "--key", KEY, "-", stdin="snap(HERE + '/tests/hand_parked.png'); result = 'ok'")
        shot = os.path.join(HERE, "tests", "hand_parked.png")
        row("run + snap while parked", rc == 0 and os.path.getsize(shot) > 1000, out)
        os.remove(shot)
        rc, out, _ = H("unpark", "--key", KEY)
        time.sleep(0.3)
        d = hand.desktop_of(pid)
        row("unpark: window back on desktop 1", rc == 0 and d == 1, f"desktop {d}: {out}")
    else:
        print("SKIP  park/unpark (no automation desktop configured)")

    rc, out, _ = H("open", base + "/app", "--key", KEY)
    row("open again: reuses the Chrome, still signed in", rc == 0 and "REUSED" in out and "VERDICT: SIGNED IN" in out
        and hand_pid() == pid, out)

    def age(s):
        s["keys"][KEY]["last_used"] = time.time() - 100000
    hand.update(age)
    end = time.time() + 40
    while time.time() < end and hand.alive(pid):
        time.sleep(1)
    row("keeper closes a window idle past the limit", not hand.alive(pid) and KEY not in hand.load()["keys"])

    rc, out, _ = H("open", base + "/app", "--key", KEY)
    row("reopen after close: the profile kept the sign-in", rc == 0 and "OPENED" in out and "VERDICT: SIGNED IN" in out, out)
    rc, out, _ = H("open", base + "/public", "--key", KEY)
    row("a page with only a Sign in link -> UNKNOWN", rc == 0 and "VERDICT: UNKNOWN" in out, out)

    rc, out, _ = H("close", "--key", KEY, "--forget")
    row("close --forget: Chrome gone, profile deleted", rc == 0 and not os.path.exists(os.path.join(hand.PROFILES, KEY))
        and KEY not in hand.load()["keys"], out)
    rc, out, _ = H("status", "--key", KEY)
    row("status of a closed key -> exit 1 NOTHING OPEN", rc == 1 and "NOTHING OPEN" in out, out)
    if not desk_was and desk.state():
        subprocess.run([sys.executable, os.path.join(HERE, "desk.py"), "--stop"], capture_output=True, cwd=HERE)
    srv.shutdown()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    unit()
    try:
        hand.check_stack(hand.machine())
    except hand.StackDown as e:
        print("CANNOT RUN the end-to-end part:", e)
        sys.exit(3)
    end_to_end()
    bad = [n for n, ok in rows if not ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} passed" + (": FAILED " + "; ".join(bad) if bad else ""))
    sys.exit(1 if bad else 0)
