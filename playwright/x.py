"""Low-level client of the driver: run one Python snippet in it and print the reply. Agents use wf.py; this is what
`wf.py run` and the driver's tests are built on.

    python x.py [--budget SECONDS] [--label TEXT] [--site KEY|URL] snippet.py     (or the snippet on stdin)
    python x.py --status          the running snippet, the kept Designer windows and the live.py runs
    python x.py --stop            ask the running snippet to stop (its next check() raises)
    python x.py --close           close the Designer windows that are open but idle

--budget defaults to 90 s (max 300). The driver always replies by budget + 10 s, so a call never hangs.
--site names the Webflow site (a sites/<key>.json key, its short name, or its Designer URL) for this snippet only.
Exit codes:
  0 ok
  1 snippet error, or stopped by --stop
  3 stack down (watchdog STOP, Chrome gone, or driver not running)
  4 over budget (if "still running", the driver stays busy until it ends)
  5 busy: another snippet is running; this one was refused, not queued
Every request carries the token the driver wrote to .driver-token-<port> when it started; a refusal (403) exits 1."""
import os, sys, json, urllib.request, urllib.error

args = sys.argv[1:]
PORT = os.environ.get("DRIVER_PORT", "8765")
URL = "http://127.0.0.1:" + PORT
TOKEN_FILE = os.environ.get("DRIVER_TOKEN_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), f".driver-token-{PORT}"))


def opt(name, default):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


def call(req, timeout):
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        print(json.loads(e.read()).get("error") if e.code == 403 else f"HTTP {e.code} from driver")
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        print(f"STACK DOWN: driver not reachable on {URL} ({getattr(e, 'reason', e)})")
        sys.exit(3)


try:
    TOKEN = open(TOKEN_FILE).read().strip()
except OSError:
    print(f"STACK DOWN: no token file {TOKEN_FILE} (driver not started on port {PORT})")
    sys.exit(3)

if "--status" in args:
    r = call(urllib.request.Request(URL + "/status", headers={"X-Token": TOKEN}), 10)
    print(("BUSY: " + r["busy_text"]) if r["busy"] else "IDLE")
    for w in r.get("windows") or []:
        print(f"Designer windows [{w['site']}]: {w['open']} open, {w['busy']} working"
              + (f", idle for {min(w['idle_s'])}-{max(w['idle_s'])}s (kept for the next command)" if w["idle_s"] else ""))
    print("last:", json.dumps(r["last"]))
    try:  # live.py runs are separate processes; listed here so one command shows everything that is running
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import live
        for lr in live.live_runs():
            print(live._run_line(lr))
    except Exception as e:
        print("live runs not listed:", e)
    sys.exit(5 if r["busy"] else 0)

if "--close" in args:
    call(urllib.request.Request(URL + "/close", data=b"", method="POST", headers={"X-Token": TOKEN}), 10)
    print("closing idle Designer windows")
    sys.exit(0)

if "--stop" in args:
    r = call(urllib.request.Request(URL + "/stop", data=b"", method="POST", headers={"X-Token": TOKEN}), 10)
    print(("stop requested: " + r["busy_text"]) if r.get("ok") else r.get("error"))
    sys.exit(0)

budget = min(int(opt("--budget", 90)), 300)
label = opt("--label", None)
site = opt("--site", None)
src = args[0] if args else None
code = open(src, encoding="utf-8").read() if src else sys.stdin.read()
headers = {"X-Budget": str(budget), "X-Label": label or (src or "stdin"), "X-Token": TOKEN}
if site:
    headers["X-Site"] = site
r = call(urllib.request.Request(URL + "/exec", data=code.encode("utf-8"), method="POST", headers=headers), budget + 30)
if r.get("stdout"):
    print(r["stdout"], end="")
if r.get("result") is not None:
    print(json.dumps(r["result"], ensure_ascii=False, indent=1, default=str)[:20000])
if r.get("secs") is not None:
    print(f"[{r['secs']}s of {budget}s budget]")
if r["ok"] and r.get("over_budget"):
    print(r["error"])
    sys.exit(4)
if not r["ok"]:
    if r.get("stack_down"):
        print("STACK DOWN: " + r["error"])
        sys.exit(3)
    print(r["error"])
    sys.exit(4 if r.get("over_budget") else 5 if r.get("busy") else 1)
