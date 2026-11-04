"""Stress test of the driver guardrails against ground truth. Runs separate driver instances (8766 real Chrome,
8767 dead CDP port) with their own STOP file, so the production driver on 8765 is untouched.
--auth-only runs just the access checks against the dead-CDP driver (no Chrome needed)."""
import http.client, json, os, random, subprocess, sys, threading, time, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__))
PW = os.path.dirname(HERE)
LOG = os.path.join(HERE, "exec.log")
STOPF = os.path.join(HERE, "STOP_test")
OUT = os.path.join(HERE, "results.json")
GRACE = 10
for f in (LOG, STOPF):
    if os.path.exists(f): os.remove(f)
R = {"cases": [], "timing": {}}


def start_driver(port, cdp):
    env = dict(os.environ, DRIVER_PORT=str(port), CDP_PORT=str(cdp), DRIVER_STOP=STOPF, PYTHONIOENCODING="utf-8")
    p = subprocess.Popen([sys.executable, os.path.join(PW, "driver.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = p.stdout.readline().strip()
    threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()
    return p, line


def wrap(cid, body):
    # every snippet logs its real start/end (ground truth for concurrency and runtime)
    return (f"import time as _t\nopen(r'{LOG}','a').write(f'S {cid} {{_t.time()}}\\n')\ntry:\n"
            + "".join("    " + l + "\n" for l in body.splitlines()) +
            f"finally:\n    open(r'{LOG}','a').write(f'E {cid} {{_t.time()}}\\n')\n")


def xpy(code, budget, label, port=8766, extra=()):
    env = dict(os.environ, DRIVER_PORT=str(port), PYTHONIOENCODING="utf-8")
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(PW, "x.py"), "--budget", str(budget), "--label", label, *extra],
                       input=code, text=True, capture_output=True, env=env, timeout=budget + 60)
    return p.returncode, round(time.time() - t0, 2), (p.stdout + p.stderr)


def tok(port):
    return open(os.path.join(PW, f".driver-token-{port}")).read().strip()


def post(code, budget, label, port=8766):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/exec", data=code.encode(), method="POST",
                                 headers={"X-Budget": str(budget), "X-Label": label, "X-Token": tok(port)})
    t0 = time.time()
    r = json.loads(urllib.request.urlopen(req, timeout=budget + 40).read())
    return t0, time.time(), r


def log_intervals():
    S, E = {}, {}
    for ln in open(LOG):
        k, cid, t = ln.split()
        (S if k == "S" else E)[cid] = float(t)
    return S, E


def wait_idle(port=8766, limit=60):
    t0 = time.time()
    while time.time() - t0 < limit:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status", headers={"X-Token": tok(port)})
        r = json.loads(urllib.request.urlopen(req, timeout=5).read())
        if not r["busy"]: return
        time.sleep(0.2)


def case(det, cid, truth, pred, **kw):
    R["cases"].append(dict(det=det, id=cid, truth=bool(truth), pred=bool(pred), **kw))


def raw(port, method, path, headers, body=b""):
    """Status code of a hand-built request (http.client, so Host/Origin/Content-Length are exactly as given)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(body or None)
        return c.getresponse().status
    except OSError:
        return "dropped"
    finally:
        c.close()


def auth_tests(port, exec_check):
    """truth = the request must be refused. exec_check: also confirm from exec.log that only allowed POSTs ran."""
    T, H = tok(port), f"127.0.0.1:{port}"
    cases = [  # (id, method, headers, truth_refused)
        ("valid", "POST", {"Host": H, "X-Token": T}, False),
        ("valid-localhost", "POST", {"Host": f"localhost:{port}", "X-Token": T}, False),
        ("valid-status", "GET", {"Host": H, "X-Token": T}, False),
        ("no-token", "POST", {"Host": H}, True),
        ("wrong-token", "POST", {"Host": H, "X-Token": "x" * len(T)}, True),
        ("token-prefix", "POST", {"Host": H, "X-Token": T[:-1]}, True),
        ("non-ascii-token", "POST", {"Host": H, "X-Token": "ä"}, True),
        ("browser-simple-post", "POST", {"Host": H, "Origin": "https://evil.example", "Content-Type": "text/plain"}, True),
        ("origin+token", "POST", {"Host": H, "Origin": "https://evil.example", "X-Token": T}, True),
        ("origin-null+token", "POST", {"Host": H, "Origin": "null", "X-Token": T}, True),
        ("rebind-host+token", "POST", {"Host": f"evil.example:{port}", "X-Token": T}, True),
        ("host-no-port+token", "POST", {"Host": "127.0.0.1", "X-Token": T}, True),
        ("no-host+token", "POST", {"X-Token": T}, True),
        ("status-no-token", "GET", {"Host": H}, True),
        ("status-rebind", "GET", {"Host": f"evil.example:{port}", "X-Token": T}, True),
    ]
    for cid, method, hdr, truth in cases:
        cid = "G-" + cid
        if method == "POST":
            body = wrap(cid, "pass").encode()
            st = raw(port, "POST", "/exec", dict(hdr, **{"Content-Length": str(len(body)), "X-Budget": "5"}), body)
        else:
            st = raw(port, "GET", "/status", hdr)
        case("auth_refused(403)", cid, truth, st == 403, status=st)
        if exec_check and method == "POST":
            wait_idle(port)
            S, _ = log_intervals() if os.path.exists(LOG) else ({}, {})
            case("auth_blocks_exec", cid, not truth, cid in S)
    for cid, size in (("huge-length", 10 ** 9), ("negative-length", -1)):
        st = raw(port, "POST", "/exec", {"Host": H, "X-Token": T, "Content-Length": str(size)})
        case("auth_refused(403)", "G-" + cid, True, st in (403, "dropped"), status=st)


def report():
    M = {}
    for c in R["cases"]:
        m = M.setdefault(c["det"], {"TP": 0, "FP": 0, "TN": 0, "FN": 0})
        m[("T" if c["truth"] == c["pred"] else "F") + ("P" if c["pred"] else "N")] += 1
    R["matrix"] = M
    json.dump(R, open(OUT, "w"), indent=1)
    for k, m in M.items(): print(f"{k:32s} {m}")
    print("FAILURES:")
    for c in R["cases"]:
        if c["truth"] != c["pred"]: print(" ", c)


import atexit
if "--auth-only" in sys.argv:
    dead, ready = start_driver(8767, 9333)
    atexit.register(dead.terminate)
    print("test driver (dead CDP):", ready, flush=True)
    auth_tests(8767, exec_check=False)
    report()
    sys.exit(0)

drv, ready = start_driver(8766, 9222)
atexit.register(drv.terminate)
print("test driver:", ready, flush=True)

# A. over-budget detection + reply bound. truth = real runtime > budget
B = 3
for mode in ("check", "nocheck"):
    for d in (0.5, 2.0, 2.8, 2.9, 2.97, 3.03, 3.1, 3.2, 4.0, 8.0, 14.0):
        cid = f"A-{mode}-{d}"
        body = (f"t0=_t.time()\nwhile _t.time()-t0<{d}:\n    _t.sleep(0.05); check()" if mode == "check" else f"_t.sleep({d})")
        code, lat, out = xpy(wrap(cid, body), B, cid)
        wait_idle()
        S, E = log_intervals()
        run = E[cid] - S[cid]
        case("over_budget(exit4)", cid, run > B, code == 4, runtime=round(run, 2), exit=code, latency=lat)
        case("reply_within_budget+grace", cid, True, lat <= B + GRACE + 1.5, latency=lat)
        if code not in (0, 4): case("no_false_error", cid, True, False, exit=code, out=out)
print("A done", flush=True)

# B. busy detection under concurrency (direct HTTP for precise send times)
reqs, lock = [], threading.Lock()
def worker(w):
    for i in range(25):
        cid = f"B-{w}-{i}"
        d = random.choice([0.05, 0.2, 0.5, 1.0])
        ts, tr, r = post(wrap(cid, f"_t.sleep({d})"), 5, cid)
        with lock: reqs.append(dict(cid=cid, ts=ts, tr=tr, busy=bool(r.get("busy")), ok=r.get("ok")))
        time.sleep(random.uniform(0, 0.6))
ths = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
def worker_low(w):   # low contention: mostly admitted, so admitted vs refused is balanced
    for i in range(30):
        cid = f"BL-{w}-{i}"
        d = random.choice([0.02, 0.05, 0.1, 0.3])
        ts, tr, r = post(wrap(cid, f"_t.sleep({d})"), 5, cid)
        with lock: reqs.append(dict(cid=cid, ts=ts, tr=tr, busy=bool(r.get("busy")), ok=r.get("ok")))
        time.sleep(random.uniform(0.2, 1.2))
ths = [threading.Thread(target=worker_low, args=(w,)) for w in range(3)]
[t.start() for t in ths]; [t.join() for t in ths]
S, E = log_intervals()
adm = [q for q in reqs if not q["busy"]]
occupied = [(q["ts"], E[q["cid"]]) for q in adm if q["cid"] in E]   # slot taken from admission to end
for q in reqs:
    if q["busy"]:
        truly = any(a - 0.03 <= q["ts"] <= b + 0.03 or a - 0.03 <= q["tr"] <= b + 0.03 for a, b in occupied)
        case("busy(exit5)", q["cid"], truly, True)
    else:
        wait = S[q["cid"]] - q["ts"]
        others = [(a, b) for (a, b), o in zip(occupied, adm) if o["cid"] != q["cid"]]
        overlap = any(a < E[q["cid"]] and S[q["cid"]] < b and not (b <= S[q["cid"]] or a >= E[q["cid"]]) for a, b in
                      [(S[o["cid"]], E[o["cid"]]) for o in adm if o["cid"] != q["cid"]])
        case("busy(exit5)", q["cid"], wait > 0.25 or overlap, False, wait=round(wait, 3), overlap=overlap)
R["timing"]["B_requests"] = len(reqs)
print("B done", len(reqs), "requests", flush=True)

# C. run_items: slow flag (truth d > per_item), budget stop, no duplicates/skips
items = [["c0", 0.2], ["c1", 0.98], ["c2", 1.02], ["c3", 2.0], ["c4", 0.5], ["c5", 1.5], ["c6", 0.9], ["c7", 1.1], ["c8", 0.3], ["c9", 3.0]]
cout, calls = os.path.join(HERE, "ri.json"), os.path.join(HERE, "ri_calls.txt")
for f in (cout, calls):
    if os.path.exists(f): os.remove(f)
snippet = (f"import time\nITEMS={items!r}\n"
           f"def fn(it):\n    open(r'{calls}','a').write(it[0]+'\\n'); time.sleep(it[1]); return {{'d': it[1]}}\n"
           f"result = run_items(ITEMS, fn, r'{cout}', key=lambda it: it[0], per_item=1.0)")
slow_reported, n = set(), 0
while True:
    n += 1
    code, lat, out = xpy(snippet, 4, f"C-call{n}")
    res = json.loads(out[out.index("{"):out.rindex("}") + 1])
    slow_reported |= {k for k, s in res["slow"]}
    case("run_items_within_budget", f"C-call{n}", True, lat <= 4 + 1.5, latency=lat)
    case("run_items_progress", f"C-call{n}", True, res["done_now"] > 0 or res["remaining"] == 0, done_now=res["done_now"])
    if res["remaining"] == 0 or n > 12: break
done = [l.strip() for l in open(calls)] if os.path.exists(calls) else []
for k, d in items:
    case("slow_flag(per_item=1.0)", k, d > 1.0, k in slow_reported, d=d)
    case("processed_exactly_once", k, True, done.count(k) == 1, count=done.count(k))
R["timing"]["C_calls"] = n
print("C done", n, "calls", flush=True)

# D. evaluate_t hard timeout (truth: JS delay > ms)
setup = "tp = globals().get('tp')\nif not tp or tp.is_closed(): tp = context.new_page(); globals()['tp'] = tp\n"
for dms in (100, 900, 960, 990, 1010, 1040, 1100, 3000):
    js = f"async () => {{ await new Promise(r => setTimeout(r, {dms})); return 'ok' }}"
    snip = setup + f"try:\n    result = evaluate_t(tp, {js!r}, None, ms=1000)\nexcept Exception as e:\n    result = 'TIMEOUT' if 'ITEM TIMEOUT' in str(e) else 'ERR ' + str(e)[:80]\n"
    code, lat, out = xpy(snip, 10, f"D-{dms}")
    case("evaluate_t_timeout(1000ms)", f"D-{dms}", dms > 1000, "TIMEOUT" in out, out=out.strip()[-60:])
xpy("tp.close()", 10, "D-close")
print("D done", flush=True)

# E. stack down: STOP file, dead CDP port, unreachable driver (truth: stack down)
open(STOPF, "w").write("test stop")
code, lat, out = xpy("result=1", 5, "E-stop"); case("stack_down(exit3)", "E-STOP-file", True, code == 3, exit=code)
os.remove(STOPF)
code, lat, out = xpy("result=1", 5, "E-after-stop"); case("stack_down(exit3)", "E-STOP-removed", False, code == 3, exit=code)
dead, ready2 = start_driver(8767, 9333)
atexit.register(dead.terminate)
for i in range(3):
    code, lat, out = xpy("result=1", 5, f"E-dead{i}", port=8767); case("stack_down(exit3)", f"E-deadCDP-{i}", True, code == 3, exit=code)
code, lat, out = xpy("result=1", 5, "E-noport", port=8799); case("stack_down(exit3)", "E-no-driver", True, code == 3, exit=code)
for c in R["cases"]:   # every exit seen in A-D with a healthy stack is a negative for stack_down
    if c.get("exit") == 3 and not c["id"].startswith("E-"): case("stack_down(exit3)", c["id"] + "-healthy", False, True)
case("stack_down(exit3)", "A-D-healthy-calls", False, any(c.get("exit") == 3 for c in R["cases"] if not c["id"].startswith("E-")))
print("E done", flush=True)

# F. status accuracy
th = threading.Thread(target=lambda: xpy(wrap("F-long", "_t.sleep(4)"), 10, "F-long")); th.start(); time.sleep(1.5)
code, lat, out = xpy("", 5, "F", extra=("--status",)); case("status_busy", "F-during", True, code == 5 and "F-long" in out, out=out.strip()[:120])
th.join()
code, lat, out = xpy("", 5, "F", extra=("--status",)); case("status_busy", "F-after", False, code == 5, out=out.strip()[:120])
print("F done", flush=True)

# G. access checks (token, Origin, Host); exec.log shows that refused snippets never ran
auth_tests(8766, exec_check=True)
print("G done", flush=True)

for p in (drv, dead): p.terminate()
report()
