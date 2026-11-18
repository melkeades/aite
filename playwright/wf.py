"""wf.py: Webflow work in the fewest steps. Collect a page's data once, answer questions from disk, write and verify
in one visit per page, finish with one QA command.

    python wf.py sync SITE [PAGE ...] [--all] [--fresh]    read pages into the store (PAGE: id, /publish/path or name)
    python wf.py pages SITE [TEXT]                         the site's pages and how old their stored data is  (offline)
    python wf.py find SITE [--text T] [--prop P] [--component C] [--page PAGE ...] [--top] [--json]      (offline)
    python wf.py find SITE --from FILE.json [--prop P] [--json]   which instance renders each finding of a list, and
                                                           with --prop whether it or its top component has such a prop
              ... --prop P --plan PLAN.json [--value V] [--task NAME]   also write the plan that sets that prop (V: JSON,
                                                           default true) on exactly the top instances behind the findings
    python wf.py find SITE --component C --props           list those instances' props                     (offline)
    python wf.py set SITE PLAN.json [--dry]                write: one visit per page checks, writes, reads back, checks
                                                           the render; --dry only resolves the plan (offline)
    python wf.py qa SITE [--task NAME] [--check JS]        end of task: reload, read every written value again, re-check
    python wf.py read SITE BODY.js PAGE [PAGE ...]         a read-only JS body per page, for what the store doesn't hold
    python wf.py live ...                                  published pages (live.py: same arguments)
    python wf.py run SITE SNIPPET.py [--budget S]          a Designer UI snippet (the old x.py)
    python wf.py status | close
    python wf.py mode "<the user's message>" [--previous wfsync|wfasync]     which mode the message asks for   (offline)

Mode: every command that opens windows takes --mode wfasync | wfsync (or the WF_MODE environment variable).
    wfasync (the default)   no concurrency: one Designer window, one live window
    wfsync                  concurrent: up to 3 Designer windows, live windows up to the machine cap
The user chooses the mode with that word in their message (router.py); the agent passes it on, it never picks one.

The usual task is four commands: sync, find, set, qa. Everything `sync` and `set` see is stored (data/<site>/), so a
second question about the same pages costs no browser step; pages read less than an hour ago are not read again.

A plan (set):
    {"task": "disable-clamp",
     "set": [{"where": {"component": "Features-6", "pages": ["/pricing"], "top": true}, "props": {"Disable line clamp": true}},
             {"page": "/about", "element": "<element id>", "props": {"<prop name or id>": null}},      null removes the override
             {"page": "/about", "element": "<text element id>", "text": "New text"}],
     "check": "(el, h) => h.all(el, '.card__p').every(n => h.css(n, '-webkit-line-clamp') === 'none')"}   optional; el = the
                                                           instance's root on the canvas; truthy = rendered as expected.
                                                           h.all(el, sel) = the VISIBLE matches (components keep hidden
                                                           copies), h.css(node, prop), h.visible(node). Return
                                                           {pass: false, ...} to have the details printed. A check
                                                           over zero matches passes with every(): require a count.
Props are set on the instance (an override). The component and its default are never touched.

Exit codes: 0 ok, 1 something failed (the rows say what), 3 driver or Chrome down, 4 out of time with pages left (run
the same command again), 5 the driver stayed busy with another agent's command."""
import json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import router  # noqa: E402

MODE = router.DEFAULT  # set by main() from --mode or WF_MODE


def mode_windows(asked=None, kind="designer"):
    """The window count this command may use under the current mode, said out loud once per command."""
    n, note = router.windows_for(MODE, asked, kind)
    print(f"mode: {MODE} ({'1 window, no concurrency' if MODE == router.ASYNC else 'concurrent, up to ' + ('3 Designer windows' if kind == 'designer' else 'the live window cap')})"
          + (f"; {note}" if note else ""), flush=True)
    try:
        with open(os.path.join(HERE, "activity.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} wf: mode {MODE}, {kind} windows {n or 'default'}\n")
    except OSError:
        pass
    return n

PORT = os.environ.get("DRIVER_PORT", "8765")
URL = "http://127.0.0.1:" + PORT
TOKEN_FILE = os.environ.get("DRIVER_TOKEN_FILE", os.path.join(HERE, f".driver-token-{PORT}"))


class Down(Exception):
    pass


def _post(path, body, headers, timeout):
    try:
        tok = open(TOKEN_FILE).read().strip()
    except OSError:
        raise Down(f"driver not started (no {TOKEN_FILE}): start it with the README 'Start' commands")
    req = urllib.request.Request(URL + path, data=body, method="POST" if body is not None else "GET",
                                 headers=dict(headers, **{"X-Token": tok}))
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        raise Down(f"driver refused the call: HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise Down(f"driver not reachable on {URL} ({getattr(e, 'reason', e)})")


def driver(code, site=None, budget=280, label="wf", wait=150):
    """Run code in the driver. When another command holds it, wait for it (up to `wait` s) instead of failing."""
    h = {"X-Budget": str(budget), "X-Label": label}
    if site:
        h["X-Site"] = site
    t0, said = time.time(), False
    while True:
        r = _post("/exec", code.encode("utf-8"), h, budget + 30)
        if not r.get("busy"):
            break
        if time.time() - t0 > wait:
            print(r.get("error"))
            sys.exit(5)
        if not said:
            print(f"waiting for the driver: {r.get('error')}", flush=True)
            said = True
        time.sleep(2)
    if r.get("stdout"):
        print(r["stdout"], end="")
    if not r["ok"]:
        if r.get("stack_down"):
            raise Down(r["error"])
        print(r.get("error"))
        sys.exit(4 if r.get("over_budget") else 1)
    return r.get("result")


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


def flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def multi(args, name):
    out = []
    while name in args:
        out.append(opt(args, name))
    return out


def unmsys(a):
    """Git Bash turns /pricing into C:/Program Files/Git/pricing before Python sees it; undo that."""
    import re
    if not os.environ.get("MSYSTEM") or not re.match(r"^[A-Za-z]:[/\\]", a):
        return a
    a2 = a.replace("\\", "/")
    root = re.sub(r"/(usr/|mingw64/)?bin/?$", "", (os.environ.get("EXEPATH") or "").replace("\\", "/"))
    for r in filter(None, [root, "C:/Program Files/Git"]):
        if a2.lower().startswith(r.lower() + "/"):
            return a2[len(r):]
    return a


def site_key(ref):
    sys.path.insert(0, HERE)
    import live
    return live.site_cfg(ref)["key"]


def ensure_site(key, fresh=False):
    s = store.load_site(key)
    if fresh or not s:
        r = driver("result = op_site()", key, 120, "wf site")
        print(f"site: {r['pages']} pages, {r['components']} components read")
        s = store.load_site(key)
    return s


def short(v, n=60):
    t = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return t if len(t) <= n else t[:n - 1] + "…"


def age(secs):
    return "never" if secs is None else f"{secs / 60:.0f} min ago" if secs < 5400 else f"{secs / 3600:.0f} h ago"


def cmd_sync(args):
    fresh, every = flag(args, "--fresh"), flag(args, "--all")
    windows = opt(args, "--windows")
    key = site_key(args.pop(0))
    refs = [unmsys(a) for a in args]
    if not refs and not every and not store.stored_pages(key):
        print("nothing to sync: name pages (id, /path or name) or use --all")
        return 1
    r = driver(f"result = op_sync({refs or None!r}, max_age={0 if fresh else None!r}, windows={mode_windows(windows)!r}, every={every!r})", key, 280, "wf sync")
    d = r.get("domains") or {}
    if d.get("known"):
        print("publish rule: " + (f"production domain {d['custom'][0]} + staging: publishing to staging is allowed without asking" if d["custom"] and d["staging"]
                                  else "only a webflow.io staging domain: ask the user before publishing" if not d["custom"] else "ask the user before publishing"))
    print(f"sync {key}: {r['asked']} pages asked, {r['fresh']} already fresh, {r['read']} read in {r['secs']}s"
          + (f" ({r['windows']} windows: opened {r['opened']}, reused {r['reused']}; {r['per_page_s']}s a page)" if r["read"] else "")
          + (f", {r['remaining']} REMAINING: run the same command again" if r["remaining"] else ""))
    for p, e in r["errors"].items():
        print(f"  FAILED {store.page_label(key, p)}: {e}")
    print(f"data: {os.path.join(store.DATA, key)}  (query it with: wf.py find {key} ...)")
    return 1 if r["errors"] else 4 if r["remaining"] else 0


def cmd_pages(args):
    key = site_key(args.pop(0))
    s = store.load_site(key)
    if not s:
        print(f"no data for {key}: wf.py sync {key} --all (or name pages)")
        return 1
    needle = (args[0] if args else "").lower()
    for p in sorted(s["pages"], key=lambda p: (p.get("kind") or "", p.get("path") or "")):
        line = f"{p['id']}  {p.get('kind') or '?':6} {'draft ' if p.get('draft') else ''}{p.get('path') or '-':60} {p.get('name')}"
        if needle in line.lower():
            print(line + f"   [data: {age(store.page_age(key, p['id']))}]")
    return 0


def cmd_locate(key, path, as_json, prop=None, plan_out=None, value=True, task=None):
    recs = json.load(open(path, encoding="utf-8"))
    if isinstance(recs, dict):  # a report's data file: take its first list of records
        recs = next((v for v in recs.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    rows = store.locate(key, recs)
    if prop:  # for each finding: does its instance have such a prop, and does the top component around it
        for r in rows:
            if not r.get("component"):
                continue
            mine = store.props_matching(key, r["page"], r["element"], prop)
            top = store.top_of(key, r["page"], r["element"])
            r["prop"] = [{"propId": p["propId"], "name": p["name"], "value": p["value"], "hasOverride": p["hasOverride"]} for p in mine]
            if top and top["element"] != r["element"]:
                tp = store.props_matching(key, r["page"], top["element"], prop)
                r["top_component"] = {"element": top["element"], "component": top["name"],
                                      "prop": [{"propId": p["propId"], "name": p["name"], "value": p["value"], "hasOverride": p["hasOverride"]} for p in tp]}
    if plan_out:
        # exactly the instances behind the findings, and only where the finding's own top-level instance has the prop
        items, seen = [], set()
        for r in rows:
            if r.get("component") and r.get("top") and len(r.get("prop") or []) == 1 and (r["page"], r["element"]) not in seen:
                seen.add((r["page"], r["element"]))
                items.append({"_page": store.page_label(key, r["page"]), "_component": r["component"], "_prop": r["prop"][0]["name"],
                              "page": r["page"], "element": r["element"], "props": {r["prop"][0]["propId"]: value}})
            r["in_plan"] = bool(r.get("component") and (r.get("page"), r.get("element")) in seen)
        with open(plan_out, "w", encoding="utf-8") as f:
            json.dump({"task": task or "task", "set": items}, f, ensure_ascii=False, indent=1)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    groups = {}
    for r in rows:
        if r["by"] == "not found":
            g = "NOT FOUND" + (f" ({r.get('error')})" if r.get("error") else "")
        elif not r.get("component"):
            g = "NO COMPONENT (a plain element of the page)"
        else:
            g = f"{r['component']}" + (f" inside {' > '.join(r['inside'])}" if r["inside"] else " (top)")
        groups.setdefault(g, []).append(r)
    for g, rs in groups.items():
        pages = {}
        for r in rs:
            pages.setdefault(r.get("page") or r["ref"], []).append(r)
        print(f"{g}: {len(rs)} finding(s) on {len(pages)} page(s)")
        if prop and rs[0].get("component"):
            def show(ps):
                return ", ".join(f"{p['name']!r} = {short(p['value'], 20)} ({'override' if p['hasOverride'] else 'default'})" for p in ps)
            r0 = rs[0]
            if r0.get("top_component"):
                t = r0["top_component"]
                print(f"    top component: {t['component']}: " + (f"has {show(t['prop'])}" if t["prop"] else f"NO prop matching {prop!r}")
                      + f"; the nested {r0['component']}: " + (f"has {show(r0['prop'])}" if r0["prop"] else f"no prop matching {prop!r}"))
            else:
                insts = {(r["page"], r["element"]): r for r in rs}
                vals = sorted({show(r["prop"]) for r in insts.values() if r["prop"]})
                print(f"    this top component ({len(insts)} instance(s) behind these findings): "
                      + ("has " + " / ".join(vals) if vals else f"NO prop matching {prop!r}"))
                if not vals:
                    i0 = store.instance(key, r0["page"], r0["element"]) or {"props": []}
                    bools = sorted({p.get("name") for p in i0["props"] if p.get("type") == "boolean" and p.get("name")})
                    print(f"      its on/off props, in case the switch has another name: {', '.join(bools) or 'none'}")
        for pid, prs in pages.items():
            els = sorted({r.get("element") or "-" for r in prs})
            print(f"    {store.page_label(key, pid) if pid in [p['id'] for p in store.load_site(key)['pages']] else pid} | element {', '.join(els)} | "
                  + "; ".join(f"#{r['i']} {r['text'][:28]!r}" + ("" if r["by"] in ("text", "no component", "not found") else f" (by {r['by']})") for r in prs[:4]) + (f" +{len(prs) - 4}" if len(prs) > 4 else ""))
    print(f"{len(rows)} finding(s) located from the store; --json gives, per finding: i, text, page, element, component, "
          "inside (component names), top, by, prop [{propId, name, value, hasOverride}], top_component, in_plan")
    if plan_out:
        n_in = sum(1 for r in rows if r.get("in_plan"))
        print(f"plan written: {plan_out}: sets the prop to {json.dumps(value)} on {len(items)} top instance(s), covering {n_in} finding(s); "
              f"{len(rows) - n_in} finding(s) are not in it (no such prop on their top component, or no component). "
              f"Add a \"check\" if you want a render check, then: wf.py set {key} {plan_out}")
    return 0


def cmd_find(args):
    as_json, top = flag(args, "--json"), flag(args, "--top")
    src, list_props = opt(args, "--from"), flag(args, "--props")
    if src:
        prop, plan_out, value, task = opt(args, "--prop"), opt(args, "--plan"), opt(args, "--value", "true"), opt(args, "--task")
        if plan_out and not prop:
            print("--plan needs --prop (which prop to set)")
            return 1
        return cmd_locate(site_key(args.pop(0)), src, as_json, prop, plan_out, json.loads(value), task)
    text, prop, comp, pages = opt(args, "--text"), opt(args, "--prop"), opt(args, "--component"), multi(args, "--page")
    key = site_key(args.pop(0))
    pids = [store.resolve_page(key, unmsys(p)) for p in pages] or None
    rows = store.find(key, text=text, prop=prop, component=comp, pages=pids, top=True if top else None)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for r in rows:
        where = f"{store.page_label(key, r['page'])} [{r['page']}]"
        inst = "NO COMPONENT (a plain element)" if not r["component"] else (
            f"{r['component']} [{r['element']}]" + (f" inside {' > '.join(r['inside'])}" if r["inside"] else " (top)"))
        if r["why"].startswith("prop"):
            what = f"prop {r['prop']!r} [{r['propId']}] = {short(r['value'])} ({'override' if r['hasOverride'] else 'default'})"
        elif r["why"] == "rendered text":
            what = f"renders {short(r['value'], 70)!r}" + (f" .{r['cls']}" if r.get("cls") else "") + (f" clamp {r['clamp']}{' CUT' if r.get('cut') else ''}" if r.get("clamp") else "")
        else:
            what = "instance"
            if list_props:
                i = store.instance(key, r["page"], r["element"])
                what = "props (group / name = value, * = override): " + "; ".join(
                    f"{(p.get('group') + ' / ') if p.get('group') else ''}{p.get('name')} [{p.get('type')}] = {short(p.get('value'), 24)}{'*' if p.get('hasOverride') else ''}" for p in i["props"])
        print(f"{where} | {inst} | {what}")
    n_pages = len(pids or store.stored_pages(key))
    print(f"{len(rows)} match(es) in {n_pages} stored page(s)" + ("" if rows else f"; stored pages may not include the one you need: wf.py sync {key} PAGE"))
    return 0


def print_rows(key, rows):
    for r in rows:
        tgt = f"{store.page_label(key, r['page'])} | {r.get('component') or 'text'} [{r['element'][-12:]}] {r.get('prop') or ''}"
        res = "ok" if r["ok"] else "FAILED"
        ovr = "" if r.get("after_override") is None else (" (override)" if r["after_override"] else " (no override)")
        rnd = "" if r.get("render") is None else (" | render ok" if r["render"] else f" | RENDER CHECK FAILED: the check returned {short(r.get('render_value'), 160)}")
        print(f"  {res:6} {tgt}: {short(r.get('old'), 30)} -> {short(r.get('after'), 30)}{ovr}{rnd}"
              + (" | WAS STALE: the stored value differed from the Designer's" if r.get("stale") else "")
              + (f" | {r['error']}" if r.get("error") else ""))


def cmd_set(args):
    dry = flag(args, "--dry")
    windows = opt(args, "--windows")
    key = site_key(args.pop(0))
    plan = json.load(open(args.pop(0), encoding="utf-8"))
    res = store.resolve_plan(key, plan)
    n = sum(len(v) for v in res["writes"].values())
    print(f"plan '{res['task']}': {n} write(s) on {len(res['writes'])} page(s)")
    for pid, ws in res["writes"].items():
        for w in ws:
            print(f"  {store.page_label(key, pid)} | {w.get('component') or 'text'} [{w['element'][-12:]}] {w.get('prop') or ''}: {short(w.get('old'), 30)} -> {short(w['value'], 30)}")
    for e in res["errors"]:
        print("  PLAN ERROR " + e)
    if res["errors"]:
        return 1
    if dry or not n:
        return 0
    r = driver(f"result = op_set({res!r}, windows={mode_windows(windows)!r})", key, 280, "wf set " + res["task"])
    print(f"set: {r['written']} written, {r['failed']} failed, {r['render_failed']} render checks failed in {r['secs']}s "
          f"({r['windows']} windows: opened {r['opened']}, reused {r['reused']})")
    print_rows(key, r["rows"])
    left = [x for x in r["rows"] if x.get("error") and "not visited" in x["error"]]
    if r["render_failed"] and not r["failed"]:
        print("the values are written; only the render check disagrees. If the check itself is wrong, fix it and pass it to qa: "
              f"wf.py qa {key} --task {res['task']} --check \"<js>\"")
    print(f"next: wf.py qa {key} --task {res['task']}   (the end-of-task check)")
    return 4 if left else 1 if r["failed"] or r["render_failed"] else 0


def cmd_qa(args):
    task, check = opt(args, "--task"), opt(args, "--check")
    key = site_key(args.pop(0))
    windows = opt(args, "--windows")
    r = driver(f"result = op_qa({task!r}, windows={mode_windows(windows)!r}, check={check!r})", key, 280, "wf qa")
    if r.get("error"):
        print(r["error"])
        return 1
    print(f"QA '{r['task']}': {'PASSED' if r['passed'] else 'FAILED'}: {r['checked']} written value(s) read again after reloading "
          f"{r['reloaded']} Designer window(s), {r['failed']} wrong, in {r['secs']}s")
    print_rows(key, [x for x in r["rows"] if not x["ok"] or x["render"] is False] or r["rows"][:3])
    if r["passed"] and len(r["rows"]) > 3:
        print(f"  ... and {len(r['rows']) - 3} more, all ok")
    print("not checked by this: the published site (publish, then check the changed pages with wf.py live)")
    return 0 if r["passed"] else 1


def cmd_read(args):
    key = site_key(args.pop(0))
    body = open(args.pop(0), encoding="utf-8").read()
    ensure_site(key)
    ids = [store.resolve_page(key, unmsys(a)) for a in args]
    r = driver(f"result = op_read({ids!r}, {body!r}, windows={mode_windows(opt(args, '--windows'))!r})", key, 280, "wf read")
    print(json.dumps(r["pages"], ensure_ascii=False, indent=1))
    return 0


def cmd_run(args):
    budget = int(opt(args, "--budget", 90))
    key = site_key(args.pop(0))
    src = args.pop(0)
    r = driver(open(src, encoding="utf-8").read(), key, min(budget, 300), os.path.basename(src))
    if r is not None:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:20000])
    return 0


def cmd_status(args):
    try:
        r = _post("/status", None, {}, 10)
        print("driver: " + (("BUSY: " + r["busy_text"]) if r["busy"] else "idle"))
        for w in r.get("windows") or []:
            print(f"  Designer windows [{w['site']}]: {w['open']} open, {w['busy']} working"
                  + (f", idle {min(w['idle_s'])}-{max(w['idle_s'])}s (kept for the next command)" if w["idle_s"] else ""))
    except Down as e:
        print(f"driver: DOWN: {e}")
    import live
    for lr in live.live_runs():
        print("  " + live._run_line(lr))
    k = live._keeper("/status")
    print(f"live windows: {len(k.get('slots') or [])} open")
    keys = sorted(os.listdir(store.DATA)) if os.path.isdir(store.DATA) else []
    for key in keys:
        ids = store.stored_pages(key)
        ages = [store.page_age(key, p) for p in ids]
        print(f"store [{key}]: {len(ids)} pages" + (f", read {age(min(ages))} to {age(max(ages))}" if ages else ""))
    if not keys:
        print("store: empty (nothing has been read yet: wf.py sync <site> <pages>)")
    return 0


def cmd_close(args):
    try:
        _post("/close", b"", {}, 10)
        print("closing idle Designer windows")
    except Down as e:
        print(e)
    import live
    k = live._keeper("/close")
    print(f"closed {k.get('closed', 0)} live window(s)")
    return 0


def main(argv):
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    global MODE
    mode = opt(args, "--mode") or os.environ.get("WF_MODE") or router.DEFAULT
    if mode.lower() not in router.MODES:
        print(f"unknown mode {mode!r}: use {router.ASYNC} or {router.SYNC}")
        return 1
    MODE = mode.lower()
    cmd = args.pop(0)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if cmd == "mode":
        prev = opt(args, "--previous")
        r = router.read_mode(" ".join(args), prev)
        if r["mode"] is None:
            print("mode: UNDECIDED: " + r["note"])
            return 1
        print(f"mode: {r['mode']} ({r['note']}): {router.obligations(r['mode'])['text']}")
        return 0
    if cmd == "live":
        import live
        if not any(a in args for a in ("--status", "--stop", "--close")):
            n = mode_windows(opt(args, "--windows"), "live")
            if n:
                args = ["--windows", str(n)] + args
        return live.main(args)
    fn = {"sync": cmd_sync, "pages": cmd_pages, "find": cmd_find, "set": cmd_set, "qa": cmd_qa, "read": cmd_read,
          "run": cmd_run, "status": cmd_status, "close": cmd_close}.get(cmd)
    if not fn:
        print(f"unknown command {cmd!r}\n" + __doc__.split("\n\n")[1])
        return 1
    try:
        return fn(args)
    except Down as e:
        print("STACK DOWN: " + str(e))
        return 3
    except (ValueError, IndexError, FileNotFoundError) as e:
        print(f"{cmd}: {e}")
        return 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
