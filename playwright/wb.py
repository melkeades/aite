"""Run JavaScript against an open Webflow Designer through relay.py, without Playwright. See README "Relay".

    python wb.py --site KEY [--timeout SECONDS] [--arg JSON] script.js      (or the script on stdin)
    python wb.py --status

The script is an async function body: `a` is the arg, `webflow`, `byId` and `tm` are available, and `return` sends back
the result. --timeout defaults to 30 s (max 300); a call that takes longer fails and blocks nothing else.
Exit codes:
  0 ok
  1 script error
  3 relay not running, or that Designer isn't connected (open_site() connects it)
  4 timeout

From Python:
    from wb import run, switch, each_page
    r = run("return await (await webflow.getCurrentPage()).getName()", site="my-site")   # {ok, v | err, ms, rt}
    result = each_page("my-site", page_ids, BODY, r"...\\out.json", arg="Component name")"""
import json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("RELAY_PORT", 8770))
URL = f"http://127.0.0.1:{PORT}"
TOKEN_FILE = os.path.join(HERE, f".relay-token-{PORT}")


def _token():
    return json.load(open(TOKEN_FILE))["control"]


def _short(site):
    """The relay names Designers by short name; accept a sites/<key>.json key or a Designer URL too."""
    if not site:
        return None
    if "//" in site or ".design." in site:
        return site.split("//")[-1].split(".design.")[0]
    path = os.path.join(HERE, "sites", site + ".json")
    return json.load(open(path, encoding="utf-8")).get("shortName", site) if os.path.exists(path) else site


def status():
    req = urllib.request.Request(URL + "/", headers={"X-Token": _token()})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def run(code, arg=None, site=None, timeout=30):
    """Run one script. Never raises for script errors or timeouts: returns {ok, v | err, ms, rt, site}; down=True
    means the relay or that Designer isn't connected, timeout=True that the call ran out of time."""
    data = json.dumps({"code": code, "arg": arg, "site": _short(site), "timeout": timeout}).encode("utf-8")
    req = urllib.request.Request(URL + "/", data=data, method="POST", headers={"X-Token": _token()})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=float(timeout) + 10).read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code} from relay: {e.read()[:200].decode('utf-8', 'replace')}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "down": True, "err": f"RELAY NOT REACHABLE on {URL}: {e}"}


def switch(site, page_id, timeout=30):
    """Switch the Designer to a page by id and wait until it is current. Returns None, or the reason it failed."""
    r = run("const p = (await webflow.getAllPagesAndFolders()).find(q => q.id === a); if (!p) throw new Error('no page ' + a);"
            " webflow.switchPage(p); return p.id", page_id, site, 20)
    if not r["ok"]:
        return r["err"]
    end = time.time() + timeout
    while time.time() < end:
        r = run("const c = await webflow.getCurrentComponent(); return !c && (await webflow.getCurrentPage()).id === a",
                page_id, site, 15)
        if r["ok"] and r["v"]:
            time.sleep(0.35)
            return None
        time.sleep(0.1)
    return f"page {page_id} not current after {timeout}s"


def each_page(site, pages, code, out, arg=None, budget=270, per_page=40):
    """Run `code` on every page id, saving to the JSON file `out` after each one; pages already saved are skipped, so
    call it again until remaining is 0. Pages slower than per_page seconds are listed under slow."""
    store = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    todo = [p for p in pages if not (p in store and "error" not in store[p])]
    start, times, slow = time.time(), [], []
    for p in todo:
        est = sum(times) / len(times) if times else 0
        if time.time() - start + est > budget:
            break
        t0 = time.time()
        err = switch(site, p)
        if err:
            v = {"error": err}
        else:
            r = run(code, arg, site, per_page)
            v = {"v": r["v"]} if r["ok"] else {"error": r["err"]}
        dt = time.time() - t0
        v["secs"] = round(dt, 2)
        store[p] = v
        times.append(dt)
        if dt > per_page:
            slow.append([p, round(dt, 1)])
        json.dump(store, open(out, "w", encoding="utf-8"), default=str)
    return {"done_now": len(times), "done_total": sum(1 for p in pages if p in store), "total": len(pages),
            "remaining": sum(1 for p in pages if p not in store), "avg_s": round(sum(times) / len(times), 2) if times else None,
            "slow": slow, "errors": [p for p in pages if "error" in store.get(p, {})]}


if __name__ == "__main__":
    args = sys.argv[1:]

    def opt(name, default=None):
        if name in args:
            i = args.index(name)
            v = args[i + 1]
            del args[i:i + 2]
            return v
        return default

    if "--status" in args:
        try:
            print(json.dumps(status()))
        except (OSError, ValueError) as e:
            print(f"RELAY NOT REACHABLE on {URL}: {e}")
            sys.exit(3)
        sys.exit(0)
    site, timeout, arg = opt("--site"), float(opt("--timeout", 30)), opt("--arg")
    code = open(args[0], encoding="utf-8").read() if args else sys.stdin.read()
    r = run(code, json.loads(arg) if arg else None, site, timeout)
    if r["ok"]:
        print(json.dumps(r.get("v"), ensure_ascii=False, indent=1))
        print(f"[{r.get('rt')} ms, {r.get('site')}]")
        sys.exit(0)
    print(r["err"])
    sys.exit(3 if r.get("down") else 4 if r.get("timeout") else 1)
