"""Watch the RTX 4090 (nvidia-smi index 1) per process. Kill the automation Chromes (the one using our chrome-profile,
the ones live.py starts and hand.py's sign-in windows) only when its own processes use the 4090: any video decode, or SM load over LIMIT%, for 3 samples in a row.
Load from other apps is logged with the process name but never kills our Chrome.
On a kill, write STOP; driver.py turns it into an error (x.py exit code 3) on the next call.
`python watchdog.py --test-kill` kills the automation Chrome and writes STOP, to test that error path.
`python watchdog.py --pids` prints the automation Chrome pids it would watch, and exits.
While running it keeps .watchdog.json ({pid, marks}): hand.py refuses to start a window the watchdog doesn't cover."""
import subprocess, time, os, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
STOP = os.path.join(HERE, "STOP")
LOG = os.path.join(HERE, "gpu.log")
PROFILE = os.path.join(HERE, "chrome-profile").lower()
LIVE_MARK = "--aite-live"  # live.py starts its own Chromes with this switch
HAND_MARK = "--aite-hand"  # hand.py starts its sign-in Chromes with this one
MARKS = ["chrome-profile", LIVE_MARK, HAND_MARK]
INFO = os.path.join(HERE, ".watchdog.json")


def automation_pids():
    """Chrome processes started with our profile or by live.py, plus all their descendants (gpu/renderer/utility)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    rows = json.loads(out) if out else []
    rows = [rows] if isinstance(rows, dict) else rows
    ours = {r["ProcessId"] for r in rows if PROFILE in (r.get("CommandLine") or "").lower()
            or LIVE_MARK in (r.get("CommandLine") or "") or HAND_MARK in (r.get("CommandLine") or "")}
    while True:
        kids = {r["ProcessId"] for r in rows if r["ParentProcessId"] in ours} - ours
        if not kids:
            return ours
        ours |= kids


def sample():
    """Return (total util, total decode, [(pid, sm, dec, name)]) for the 4090."""
    tot = subprocess.run(["nvidia-smi", "-i", "1", "--query-gpu=utilization.gpu,utilization.decoder",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
    util, dec = (int(x) for x in tot.split(","))
    out = subprocess.run(["nvidia-smi", "pmon", "-i", "1", "-c", "1", "-s", "u"],
                         capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        p = line.split()
        if len(p) < 10 or p[0].startswith("#"):
            continue
        n = lambda v: int(v) if v.isdigit() else 0
        procs.append((int(p[1]), n(p[3]), n(p[6]), " ".join(p[9:])))
    return util, dec, procs


def kill_chrome():
    pids = automation_pids()
    if pids:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Stop-Process -Force -Id " + ",".join(map(str, pids))], capture_output=True)


if "--test-kill" in sys.argv:
    kill_chrome()
    open(STOP, "w").write(f"TEST kill (watchdog.py --test-kill) at {time.strftime('%H:%M:%S')}\n")
    print("test kill done", flush=True)
    sys.exit(0)

if "--pids" in sys.argv:
    print(json.dumps(sorted(automation_pids())))
    sys.exit(0)

with open(INFO, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "marks": MARKS, "limit": LIMIT, "at": time.time()}, f)
bad = 0
peak = 0
ours, tick, fails = set(), 0, 0
while not os.path.exists(STOP):
    tick += 1
    try:
        if tick % 5 == 1:
            ours = automation_pids()
        util, dec, procs = sample()
        fails = 0
    except Exception as e:
        # nvidia-smi sometimes returns blank fields; skip the sample, but fail closed if it never recovers
        fails += 1
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} sample failed ({fails}): {e!r}\n")
        if fails >= 30:
            kill_chrome()
            open(STOP, "w").write(f"watchdog blind: {fails} failed samples in a row, last {e!r} "
                                  f"at {time.strftime('%H:%M:%S')}\n")
            print("STOPPED blind", flush=True)
            break
        time.sleep(1)
        continue
    peak = max(peak, util)
    mine = [p for p in procs if p[0] in ours]
    my_sm = max((p[1] for p in mine), default=0)
    my_dec = max((p[2] for p in mine), default=0)
    others = [f"{p[3]}:{p[0]} sm={p[1]} dec={p[2]}" for p in procs if p[0] not in ours and (p[1] or p[2])]
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} util={util} dec={dec} ours(sm={my_sm} dec={my_dec})"
                + (f" others: {'; '.join(others)}" if others else "") + "\n")
    bad = bad + 1 if (my_dec > 0 or my_sm > LIMIT) else 0
    if bad >= 3:
        kill_chrome()
        open(STOP, "w").write(f"automation Chrome used the 4090: sm={my_sm} dec={my_dec} "
                              f"pids={[p[0] for p in mine]} at {time.strftime('%H:%M:%S')}\n")
        print("STOPPED", my_sm, my_dec, flush=True)
        break
    time.sleep(1)
try:
    os.remove(INFO)
except OSError:
    pass
print("watchdog exit, peak", peak, flush=True)
