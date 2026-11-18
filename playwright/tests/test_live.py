"""live.py against ground truth: a local HTTP server whose pages are slow, missing, never answer or hang the page.

    python tests/test_live.py

Checks that the windows really work at the same time (the server counts overlapping requests), that every kind of
failed load is an error and never a silent pass, that a hung page's window is replaced and the run goes on, that budgets
and resume work, that windows stay open and are reused by the next run, that the window cap refuses, that a killed or
stopped run frees its windows, and that --close leaves no Chrome behind. Needs the watchdog running
(or "watchdog": false in machine.json). Prints PASS/FAIL per check and exits 1 on any FAIL."""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
PW = os.path.dirname(HERE)
sys.path.insert(0, PW)
import live  # noqa: E402

results, active, peak, lock = [], [0], [0], threading.Lock()
TMP = tempfile.mkdtemp(prefix="live-test-")


def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(info)[:400]}]" if info and not ok else ""), flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/hang"):
            time.sleep(60)  # never answers in time
            return
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if path.startswith("/slow"):
                time.sleep(1.5)
            code = 404 if path.startswith("/missing") else 500 if path.startswith("/boom") else 200
            body = f"<!doctype html><title>{path}</title><h1 style='height:3000px'>{path}</h1>".encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with lock:
                active[0] -= 1


class Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # a closed window resets its connections


srv = Quiet(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
U = lambda p: BASE + p


def marked_chromes():
    ps = ("@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | ? CommandLine -match '--aite-live').Count")
    return int(subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip() or 0)


def cli(args, budget=120, env=None):
    p = subprocess.run([sys.executable, os.path.join(PW, "live.py")] + args, capture_output=True, text=True,
                       timeout=budget, encoding="utf-8", env=dict(os.environ, **(env or {})))
    return p.returncode, p.stdout + p.stderr


def run(items, each=None, **k):
    """live.run, returning (summary, raised PagesFailed or None)."""
    try:
        return live.run(None, items, each, quiet=True, **k), None
    except live.PagesFailed as e:
        return e.summary, e


# 1. the windows work at the same time
cli(["--close"])
base = marked_chromes()
peak[0] = 0
t0 = time.time()
s, err = run([U(f"/slow/{i}") for i in range(6)], windows=3)
wall = time.time() - t0
check("6 slow pages in 3 windows: all done, no errors", s["done_total"] == 6 and not s["errors"], s)
check("3 requests overlapped on the server", peak[0] >= 3, peak[0])
check("faster than one after another (6 x 1.5 s server delay)", s["wall_s"] < 9 + 6, s["wall_s"])
check("first run opened its 3 windows", s["windows_opened"] == 3 and s["windows_reused"] == 0, s)
s2, err = run([U(f"/ok/again{i}") for i in range(3)], windows=3)
check("the next run reuses them: nothing opened, no new Chrome", s2["windows_opened"] == 0 and s2["windows_reused"] == 3 and marked_chromes() == base + 3, (s2, marked_chromes()))

# 2. every failed load is an error you can catch; the others go on
out = os.path.join(TMP, "fail.json")
dead = "http://127.0.0.1:9/"  # nothing listens there
s, err = run([U("/ok/a"), U("/missing/x"), U("/boom/x"), dead, U("/hang/x"), U("/ok/b")], windows=3, out=out, load_timeout=6)
R = s["results"]
check("PagesFailed raised after the run", isinstance(err, live.PagesFailed) and "3 of" not in str(err) and len(s["errors"]) == 4, s["errors"])
check("404 is a load error with its status", R[U("/missing/x")].get("kind") == "load" and R[U("/missing/x")].get("status") == 404, R[U("/missing/x")])
check("500 is a load error", "HTTP 500" in R[U("/boom/x")].get("error", ""), R[U("/boom/x")])
check("connection refused is a load error", "LOAD FAILED" in R[dead].get("error", ""), R[dead])
check("a page that never answers is LOAD TIMEOUT, not a hang", "LOAD TIMEOUT" in R[U("/hang/x")].get("error", "") and R[U("/hang/x")]["secs"] < 12, R[U("/hang/x")])
check("the good pages were still done", "v" in R[U("/ok/a")] and "v" in R[U("/ok/b")], R)

# 3. resume: saved pages are skipped, failed ones retried
s, err = run([U("/ok/a"), U("/missing/x"), U("/ok/b")], windows=2, out=out)
check("resume skips saved items and retries the failed one", s["done_now"] == 1 and list(s["errors"]) == [U("/missing/x")], s)


# 4. inside each(): goto raises LoadError (catchable); an exception is that item's error; evaluate times out
def each(p, item):
    if "goto" in item:
        try:
            p.goto(U("/missing/inner"))
        except live.LoadError as e:
            return {"caught": e.status}
    if "raise" in item:
        raise ValueError("boom in each")
    if "evalto" in item:
        return p.evaluate("() => new Promise(() => {})", timeout=2)
    if "narrow" in item["path"]:
        shot = p.snap(os.path.join(TMP, "s.png"), full=True)
        return {"iw": p.evaluate("innerWidth"), "shot": os.path.getsize(shot)}
    return 1


s, err = run([{"path": U("/ok/goto"), "key": "goto"}, {"path": U("/ok/raise"), "key": "raise"},
              {"path": U("/ok/evalto"), "key": "evalto"}, {"path": U("/ok/narrow"), "width": 390, "key": "narrow"}],
             lambda p, item: each(p, item["key"] if item["key"] != "narrow" else item), windows=3)
R = s["results"]
check("p.goto() in each raises LoadError with the status", R["goto"].get("v") == {"caught": 404}, R["goto"])
check("an exception in each is that item's error", R["raise"].get("kind") == "each" and "boom in each" in R["raise"]["error"], R["raise"])
check("p.evaluate raises after its timeout", "EVALUATE TIMEOUT" in R["evalto"].get("error", ""), R["evalto"])
check("item width 390 lays out at 390 and snap writes a file", R["narrow"].get("v", {}).get("iw") == 390 and R["narrow"]["v"]["shot"] > 1000, R["narrow"])


# 5. a page that hangs its window is killed at per_page; the window reopens and the run goes on
def hang(p, item):
    if "freeze" in item:
        p.note("freezing")
        p.page.evaluate("() => { while (true) {} }")
    return "fine"


t0 = time.time()
s, err = run([U("/ok/freeze"), U("/ok/1"), U("/ok/2"), U("/ok/3"), U("/ok/4")], hang, windows=2, per_page=8)
R = s["results"]
check("hung page: PAGE TIMEOUT at per_page", R[U("/ok/freeze")].get("kind") == "timeout", R[U("/ok/freeze")])
check("hung page: the other 4 items still done", sum(1 for k, v in R.items() if v.get("v") == "fine") == 4, R)
check("hung page: run ended soon after per_page", time.time() - t0 < 40, time.time() - t0)

# 6. budget: stops cleanly with items left (exit 4), the same command finishes them (exit 0)
out = os.path.join(TMP, "budget.json")
args = ["--out", out, "--windows", "2", "--budget", "5"] + [U(f"/slow/b{i}") for i in range(12)]
code, text = cli(args)
left = json.loads(text[text.index("{"):text.rindex("}") + 1])["remaining"]
check("budget 5 s: exit 4 with items remaining", code == 4 and 0 < left < 12, (code, text[-300:]))
code2, text2 = cli(args[:5] + ["60"] + args[6:])
check("same command again: exit 0, nothing remaining", code2 == 0 and '"remaining": 0' in text2, (code2, text2[-300:]))

# 7. command line: failed items exit 1 and are listed; --js saves what the script returns
jsf = os.path.join(TMP, "t.js")
open(jsf, "w").write("() => document.title")
out = os.path.join(TMP, "cli.json")
code, text = cli(["--out", out, "--js", jsf, U("/ok/t1"), U("/missing/t2")])
saved = json.load(open(out, encoding="utf-8"))
check("cli: exit 1 and FAILED ITEMS listed", code == 1 and "FAILED ITEMS (1)" in text, (code, text[-300:]))
check("cli --js: result saved", saved[U("/ok/t1")].get("v") == "/ok/t1", saved)

# 8. the window cap refuses; a killed run frees its windows for the next run; --stop and --close
def keeper_slots():
    return live._keeper("/status").get("slots") or []


slow_items = [U(f"/slow/k{i}") for i in range(40)]
bg = subprocess.Popen([sys.executable, os.path.join(PW, "live.py"), "--windows", "3", "--budget", "100"] + slow_items,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
end = time.time() + 30
while sum(1 for x in keeper_slots() if x["holder"] == bg.pid) < 3 and time.time() < end:
    time.sleep(0.5)
check("a run is listed while it works", any(r["pid"] == bg.pid for r in live.live_runs()), live.live_runs())
code, text = cli([U("/ok/cap")], env={"LIVE_MAX_WINDOWS": "3"})
check("window cap reached: exit 5 BUSY", code == 5 and "BUSY" in text, (code, text[-300:]))
n_before = marked_chromes()
subprocess.run(["taskkill", "/F", "/PID", str(bg.pid)], capture_output=True)  # the process only, not its tree
end = time.time() + 10
while any(x["holder"] for x in keeper_slots()) and time.time() < end:
    time.sleep(0.5)
check("hard-killed run: its windows are free again and still open", not any(x["holder"] for x in keeper_slots()) and marked_chromes() == n_before, (keeper_slots(), marked_chromes()))
check("hard-killed run: no longer listed", not any(r["pid"] == bg.pid for r in live.live_runs()))
s3, err = run([U("/ok/after-kill")], windows=1)
check("the run after a killed one works in a kept window", s3["windows_reused"] == 1 and not s3["errors"], s3)

bg = subprocess.Popen([sys.executable, os.path.join(PW, "live.py"), "--windows", "2", "--budget", "100"] + slow_items,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
end = time.time() + 30
while sum(1 for x in keeper_slots() if x["holder"] == bg.pid) < 2 and time.time() < end:
    time.sleep(0.5)
code, text = cli(["--stop", str(bg.pid)])
time.sleep(3)
check("--stop: the run is gone, its windows stay", bg.poll() is not None and marked_chromes() == n_before and not any(x["holder"] for x in keeper_slots()), (text, marked_chromes(), keeper_slots()))
code, text = cli(["--close"])
end = time.time() + 10
while marked_chromes() > base and time.time() < end:
    time.sleep(1)
check("--close: every live Chrome is gone", marked_chromes() == base, (text, marked_chromes(), base))

bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)} of {len(results)} passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
