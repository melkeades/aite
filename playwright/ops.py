"""Designer operations for wf.py. driver.py loads this after bridge.py, into the same snippet globals.

One rule shapes everything here: a Designer page costs 2.5-5 s to switch to and 0.2 s to read completely, so a page is
visited ONCE per operation and that visit does everything: read every instance, prop and text; write; read back; look
at what the canvas renders. What a visit learns goes to the store (store.py), so the next question about that page is
answered from disk.

    op_site()                      pages and components of the site, no page switch (about 2 s)
    op_sync(page_ids, max_age)     visit the pages whose stored data is older than max_age
    op_set(resolved_plan)          visit each page of the plan: check the old values, write, read back, check the render
    op_qa(task)                    visit the task's pages in a fresh window state: are the values saved, does it render

Visits run in the kept Designer windows (bridge.py _pool), several pages at the same time, one page per window. The
driver thread only starts a visit in a window and polls it, so all windows work at once."""
import json as _json, os, sys, time

if (globals().get("HERE") or "") not in sys.path:
    sys.path.insert(0, globals().get("HERE") or os.getcwd())
import store as _store
import importlib as _importlib
_importlib.reload(_store)

# ---- what runs inside the bridge frame for one page visit ----
_VISIT = r"""
window.__wfVisit = async (spec) => {
  const T = {}, t0 = performance.now(), sleep = ms => new Promise(r => setTimeout(r, ms));
  const pg = (await webflow.getAllPagesAndFolders()).find(q => q.id === spec.page);
  if (!pg) throw new Error('no page ' + spec.page);
  const here = async () => (await webflow.getCurrentPage()).id === spec.page && !(await webflow.getCurrentComponent());
  if (!(await here())) {
    webflow.switchPage(pg);                       // never awaited: awaiting it can hang
    let ok = false;
    for (let i = 0; i < 400 && !ok; i++) { ok = await here(); if (!ok) await sleep(50); }
    if (!ok) throw new Error('page not current after 20 s');
  }
  T.switch = Math.round(performance.now() - t0);
  let els = await webflow.getAllElements();
  const byId = id => els.find(e => e.id.element === id);

  // ---- writes: compare with what the plan expects, write, read back ----
  const writes = [];
  for (const w of spec.writes || []) {
    const r = { element: w.element, kind: w.kind, propId: w.propId, prop: w.prop, value: w.value };
    try {
      const el = byId(w.element);
      if (!el) throw new Error('element not on this page');
      if (w.kind === 'text') {
        r.before = await el.getText();
        if (!spec.verify) await el.setText(w.value);
        r.after = await el.getText(); r.ok = r.after === w.value;
      } else {
        const read = async () => { const p = (await el.getProps()).find(q => q.propId === w.propId); return p ? [p.value, p.hasOverride] : null };
        const b = await read();
        if (!b) throw new Error('prop not on this instance');
        r.before = b[0]; r.before_override = b[1];
        r.stale = !spec.verify && w.old !== undefined && JSON.stringify(b[0]) !== JSON.stringify(w.old);
        if (!spec.verify) await el.setProps([{ propId: w.propId, value: w.value }]);
        const a = await read();
        r.after = a[0]; r.after_override = a[1];
        // null means "remove the override": then the instance must show no override
        r.ok = w.value === null ? a[1] === false : (JSON.stringify(a[0]) === JSON.stringify(w.value) && a[1] === true);
      }
    } catch (e) { r.ok = false; r.error = String(e && e.message || e).slice(0, 200); }
    writes.push(r);
  }
  T.writes = Math.round(performance.now() - t0) - T.switch;

  // ---- snapshot: every component instance with every prop, every text element, what sits inside what ----
  let snap = null;
  if (spec.read) {
    if (writes.length) els = await webflow.getAllElements();
    const names = {};
    const inst = els.filter(e => e.type === 'ComponentInstance');
    const rows = await Promise.all(inst.map(async (e, order) => {
      const c = await e.getComponent();
      const [name, defs, vals] = await Promise.all([c.getName(), c.getProps(), e.getProps()]);
      names[e.id.element] = name;
      const m = Object.fromEntries(defs.map(d => [d.id, d]));
      return { order, element: e.id.element, component: name, component_id: c.id, name,
        props: vals.map(v => ({ propId: v.propId, name: m[v.propId]?.name, group: m[v.propId]?.group, type: m[v.propId]?.type, value: v.value, hasOverride: v.hasOverride, defaultValue: m[v.propId]?.defaultValue })) };
    }));
    const strs = els.filter(e => e.type === 'String');
    const texts = await Promise.all(strs.map(e => e.getText().catch(() => null)));
    const safe = async f => { try { return await f() } catch (e) { return null } };
    snap = { page: { id: pg.id, name: await safe(() => pg.getName()), slug: await safe(() => pg.getSlug()), path: pg.getPublishPath ? await safe(() => pg.getPublishPath()) : null, kind: pg.getKind ? await safe(() => pg.getKind()) : null },
             elements: els.length, instances: rows, strings: strs.map((e, i) => ({ element: e.id.element, text: texts[i] })) };
  }
  T.read = Math.round(performance.now() - t0) - T.switch - T.writes;
  let v = spec.body ? await (new (async () => {}).constructor('a', 'els', spec.body))(spec.arg, els) : undefined;
  T.total = Math.round(performance.now() - t0);
  return { T, writes, snap, v };
};
window.__wfStart = (spec) => { const j = (window.__wfJob = { done: false, page: spec.page });
  window.__wfVisit(spec).then(v => { j.v = v; j.done = true }, e => { j.err = String(e && e.message || e).slice(0, 300); j.done = true }); return 1 };
"""

# ---- what runs in the canvas of the same window, right after the bridge part of the visit ----
# Every rendered node knows which page-level instance it belongs to (data-wf-id is the path of element ids), so one
# pass maps all visible text to its instance: this is what finds the component of CMS-bound text.
_CANVAS = r"""async (x) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const __check = __CHECK__;
  const rendered = [];
  const t0 = performance.now(); let settle = 0, polls = [];
  if (x.read) { let last = -1, same = 0;
    for (let i = 0; i < 30 && same < 3; i++) { const n = document.querySelectorAll('[data-wf-id]').length; polls.push(n); same = n === last ? same + 1 : 0; last = n; if (same < 3) await sleep(150); }
    settle = Math.round(performance.now() - t0); }
  if (x.read) for (const e of document.querySelectorAll('[data-wf-id]')) {
    if (e.childElementCount) continue;
    const tx = (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    if (tx.length < 3) continue;
    let path; try { path = JSON.parse(e.getAttribute('data-wf-id')) } catch (er) { continue }
    const cs = getComputedStyle(e), lc = cs.webkitLineClamp, clamp = lc && lc !== 'none' ? lc : null;
    const r = e.getBoundingClientRect();
    rendered.push({ instance: path[0], path, cls: (typeof e.className === 'string' ? e.className : '').split(/\s+/).filter(c => c && !c.startsWith('w-variant')).join('.'),
      text: tx.slice(0, 200), clamp, cut: !!clamp && e.scrollHeight - e.clientHeight > 2, shown: r.width > 0 && r.height > 0 });
  }
  // which page instance sits inside which: the ancestors of each instance's root node that are instances themselves
  const nest = {};
  if (x.instances) { const ids = new Set(x.instances);
    for (const id of x.instances) { const el = document.querySelector(`[data-w-id="${id}"], [data-w-id^="${id}_instance-"]`);
      if (!el) { nest[id] = null; continue }
      const chain = []; let p = el.parentElement;
      while (p) { const w = p.getAttribute && p.getAttribute('data-w-id'); if (w) { const b = w.split('_instance-')[0]; if (ids.has(b) && b !== id) chain.unshift(b) } p = p.parentElement }
      nest[id] = chain; } }
  const checks = [];
  // helpers for a check: visible nodes only (components often keep hidden placeholder copies), and computed style
  const H = { visible: n => !!(n && n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden'),
              all: (root, sel) => [...root.querySelectorAll(sel)].filter(n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden'),
              css: (n, p) => getComputedStyle(n).getPropertyValue(p) };
  if (__check) { const f = __check;
    for (const id of x.elements || []) { let v, err = null;
      const ok = r => !!r && !(typeof r === 'object' && 'pass' in r && !r.pass);
      for (let i = 0; i < 12; i++) {   // the canvas re-renders a moment after a write: wait up to 1.5 s for a passing answer
        // inside a collection list the instance is rendered once per item, each copy as "<id>_instance-<n>": check them all
        const roots = [...document.querySelectorAll(`[data-w-id="${id}"], [data-w-id^="${id}_instance-"]`)];
        err = roots.length ? null : 'instance not rendered on the canvas'; v = null;
        let threw = false;
        if (roots.length) { try { const rs = []; for (const el of roots) rs.push(await f(el, H));
            const bad = rs.filter(r => !ok(r));
            v = roots.length === 1 ? rs[0] : (bad.length ? { pass: false, copies: roots.length, failed: bad.length, first_failed: bad[0] === undefined ? null : bad[0] }
                                                         : { pass: true, copies: roots.length, first: rs[0] });
          } catch (er) { err = String(er.message || er).slice(0, 160); v = null; threw = true } }
        if (threw || (ok(v) && !err)) break;
        await sleep(125); }
      const pass = !!v && !err && !(typeof v === 'object' && 'pass' in v && !v.pass);
      checks.push({ element: id, value: v === undefined ? null : v, error: err, pass }); } }
  return { rendered, checks, nest, T: { settle, polls: polls.slice(0, 30), total: Math.round(performance.now() - t0) } };
}"""
_SITE = r"""
const all = await webflow.getAllPagesAndFolders();
const safe = async f => { try { return await f() } catch (e) { return null } };
const pages = await Promise.all(all.map(async p => ({ id: p.id, type: p.type, name: await safe(() => p.getName()), slug: await safe(() => p.getSlug()),
  path: p.getPublishPath ? await safe(() => p.getPublishPath()) : null, kind: p.getKind ? await safe(() => p.getKind()) : null,
  draft: p.isDraft ? await safe(() => p.isDraft()) : null, collection: p.getCollectionId ? await safe(() => p.getCollectionId()) : null,
  utility: p.getUtilityPageKey ? await safe(() => p.getUtilityPageKey()) : null })));
const comps = await webflow.getAllComponents();
const components = await Promise.all(comps.map(async c => ({ id: c.id, name: await c.getName(), instances: c.getInstanceCount ? await safe(() => c.getInstanceCount()) : null,
  props: ((await safe(() => c.getProps())) || []).map(d => ({ id: d.id, name: d.name, type: d.type, group: d.group, defaultValue: d.defaultValue })) })));
const info = await safe(() => webflow.getSiteInfo());
return { pages: pages.filter(p => !/folder/i.test(p.type || '')), components, info };
"""


_REG = os.path.join(globals().get("HERE") or os.getcwd(), ".session-windows.json")


def _registry_save():
    """Remember the session windows on disk, so a restarted driver can close the ones the old driver left open."""
    try:
        with open(_REG, "w") as f:
            _json.dump([w["tid"] for ws in _pool().values() for w in ws], f)
    except OSError:
        pass


def _close_leftovers():
    """Close Designer windows a previous driver process opened for its session and never closed (a driver restart
    forgets its windows; left open, they keep rendering and slow every visit down)."""
    try:
        old = set(_json.load(open(_REG)))
    except Exception:
        return 0
    mine = {w["tid"] for ws in _pool().values() for w in ws}
    n = 0
    for p in list(context.pages):
        try:
            if _target_id(p) in old - mine:
                p.close()
                n += 1
        except Exception:
            pass
    if n:
        _log(f"designer: closed {n} window(s) left open by an earlier driver process")
    _registry_save()
    return n


def _session(n):
    """n kept Designer windows of this snippet's site, bridge running: reuse the free ones, open the missing ones."""
    cfg = site()
    if not cfg:
        raise RuntimeError("no site: pass --site KEY")
    _close_leftovers()
    wins = _pool().setdefault(cfg["key"], [])
    ok = []
    for w in list(wins):
        if w["job"] is not None:
            continue
        try:
            alive = (not w["tab"].is_closed()) and _host(w["tab"].url).endswith(DESIGN)
        except Exception:
            alive = False
        if alive:
            ok.append(w)
        else:
            _pool_close(w)
            wins.remove(w)
    use_, fresh = ok[:n], []
    for i in range(n - len(use_)):
        progress(f"opening Designer window {len(use_) + i + 1} of {n}")
        d, tid = _job_new_window(cfg, f"about:blank#wf-{int(time.time())}-{i}")
        w = {"tab": d, "tid": tid, "last": time.time(), "job": None}
        wins.append(w)
        fresh.append(w)
    if fresh:
        tile_windows([w["tab"] for w in use_ + fresh])
        _wait_designers([w["tab"] for w in fresh])
    mine = use_ + fresh
    _launch_apps([w["tab"] for w in mine])  # no-op for a window whose bridge app is already running
    for w in mine:
        w["job"] = "wf"
    _registry_save()
    _log(f"designer: wf session [{cfg['key']}]: opened {len(fresh)} window(s), reused {len(use_)}")
    return cfg, mine, {"opened": len(fresh), "reused": len(use_)}


def _release(mine):
    for w in mine:
        w["job"], w["last"] = None, time.time()


def _windows_for(n_pages, asked=None):
    # measured on 11 pages: 1 window 104 s, 2 windows 72 s, 3 windows 48 s. So one window per page, up to three.
    return max(1, min(asked or 3, 3, n_pages))


def _canvas_of(tab):
    return next((f for f in tab.frames if f.name == "site-iframe-next"), None)


def _visits(specs, windows=None, read=True, check_js=None, on_page=None):
    """Run one visit per spec ({"page", "writes"?, "verify"?, "body"?, "arg"?}) across the session's windows, each
    window on its own page at the same time. Returns {page_id: result}; a result has T, writes, snap, rendered,
    checks, or error. Stops handing out pages when the snippet's budget is nearly used (the rest come back missing)."""
    if not specs:
        return {}, {"opened": 0, "reused": 0, "windows": 0}
    cfg, mine, how = _session(_windows_for(len(specs), windows))
    out, queue, active = {}, list(specs), {}
    t_avg = []
    try:
        for w in mine:
            _bridge_of(w["tab"]).evaluate("(() => {" + _VISIT + "})()")
        st = cur()
        while queue or active:
            for w in mine:
                if id(w) not in active and queue:
                    left = st["budget"] - (time.time() - st["start"]) if st else 999
                    if t_avg and left < max(t_avg) + 6:
                        queue.clear()  # no time for another page: the caller runs again for the rest
                        break
                    spec = dict(queue.pop(0), read=read)
                    _bridge_of(w["tab"]).evaluate("s => window.__wfStart(s)", spec)
                    active[id(w)] = (w, spec, time.time())
            for key, (w, spec, t0) in list(active.items()):
                res = w.get("_res")
                if res is None:  # phase 1: the bridge part (switch, write, read)
                    try:
                        j = _bridge_of(w["tab"]).evaluate("() => window.__wfJob && window.__wfJob.done ? window.__wfJob : null")
                    except Exception as e:
                        j = {"err": f"window lost: {str(e).splitlines()[0][:120]}"}
                    if not j and time.time() - t0 > 60:
                        j = {"err": "visit did not finish in 60 s"}
                    if not j:
                        continue
                    res = {"error": j["err"]} if j.get("err") else j["v"]
                    if "error" not in res:
                        try:  # phase 2: the canvas part, started here and collected on a later tick
                            els = [x["element"] for x in res.get("writes") or []]
                            fn = _CANVAS.replace("__CHECK__", "(" + check_js + ")" if check_js else "null")
                            _canvas_of(w["tab"]).evaluate("x => { const j = (window.__wfC = { done: false }); (" + fn + ")(x).then(v => { j.v = v; j.done = true }, e => { j.err = String(e && e.message || e).slice(0, 200); j.done = true }); return 1 }",
                                                          {"read": read, "elements": els,
                                                           "instances": [i["element"] for i in (res.get("snap") or {}).get("instances", [])]})
                            w["_res"], w["_c0"] = res, time.time()
                            continue
                        except Exception as e:
                            res["rendered"], res["checks"], res["canvas_error"] = [], [], str(e).splitlines()[0][:160]
                else:
                    try:
                        c = _canvas_of(w["tab"]).evaluate("() => window.__wfC && window.__wfC.done ? window.__wfC : null")
                    except Exception as e:
                        c = {"err": str(e).splitlines()[0][:160]}
                    if not c and time.time() - w["_c0"] > 15:
                        c = {"err": "canvas did not answer in 15 s"}
                    if not c:
                        continue
                    if c.get("err"):
                        res["rendered"], res["checks"], res["canvas_error"] = [], [], c["err"]
                    else:
                        res["rendered"], res["checks"] = c["v"]["rendered"], c["v"]["checks"]
                        if res.get("snap"):
                            rows, nest = res["snap"]["instances"], c["v"].get("nest") or {}
                            name = {i["element"]: i["name"] for i in rows}
                            for i in rows:
                                i["inside"] = [{"element": x, "name": name.get(x)} for x in (nest.get(i["element"]) or [])]
                                i["on_canvas"] = nest.get(i["element"]) is not None
                            for i in rows:  # an instance that holds most of the page's other instances is the page shell
                                i["wraps_page"] = len(rows) > 3 and sum(1 for x in rows if any(c2["element"] == i["element"] for c2 in x["inside"])) >= len(rows) * 0.6
                        res["T"]["canvas"] = c["v"].get("T")
                w.pop("_res", None)
                del active[key]
                res["secs"] = round(time.time() - t0, 2)
                t_avg.append(res["secs"])
                out[spec["page"]] = res
                if on_page:
                    on_page(spec["page"], res)
                progress(f"{len(out)}/{len(specs)} pages")
            check()
            mine[0]["tab"].wait_for_timeout(40)
    finally:
        for w in mine:
            w.pop("_res", None)
        _release(mine)
    return out, dict(how, windows=len(mine))


def _domains(s):
    """What the site can be published to, from the Designer's site info: decides the publish rule."""
    info = (s or {}).get("info") or {}
    doms = info.get("domains") or []
    custom = [d.get("url") for d in doms if d.get("url") and not str(d.get("url")).endswith(".webflow.io")]
    return {"custom": custom, "staging": [d.get("url") for d in doms if str(d.get("url") or "").endswith(".webflow.io")],
            "known": bool(info)}


def _save_page(key, pid, res):
    s = res["snap"]
    ids = {i["element"] for i in s["instances"]}
    for r in res.get("rendered") or []:
        # the node's path lists element ids from the page down; the first one that is a component instance of this
        # page is the instance that renders it (inside a collection list the path starts with the list's own element)
        path = r.pop("path", None) or [r["instance"]]
        # (a node inside a collection list item carries "<element id>_instance-<n>")
        r["instance"] = next((y for y in (x.split("_instance-")[0] for x in path) if y in ids), None)
        if r["instance"] is None:  # text that no component instance renders: a plain element of the page
            r["root"], r["path"] = path[0], path
    _store.save_page(key, pid, {"page": s["page"], "elements": s["elements"], "instances": s["instances"],
                                 "strings": s["strings"], "rendered": res.get("rendered") or [],
                                 "visit": dict(res.get("T") or {}, secs=res.get("secs"))})


def op_site():
    """Pages and components of the site into the store; no page switch. Uses any open Designer of the site."""
    cfg = site()
    wins = [w for w in _pool().get(cfg["key"], []) if w["job"] is None and _pool_ok(w)]
    if wins:
        r = evaluate_t(_bridge_of(wins[0]["tab"]), "async () => {" + _SITE + "}", None, ms=40000)
    else:
        cfg2, mine, how = _session(1)
        try:
            r = evaluate_t(_bridge_of(mine[0]["tab"]), "async () => {" + _SITE + "}", None, ms=40000)
        finally:
            _release(mine)
    _store.save_site(cfg["key"], r)
    return {"pages": len(r["pages"]), "components": len(r["components"])}


def op_sync(refs=None, max_age=None, windows=None, every=False):
    """Bring the store up to date: the site's page and component lists, then a visit to every asked page whose data
    is older than max_age seconds. refs: page ids, publish paths or names (None = pages already in the store; every =
    all pages that aren't drafts or utility pages). Returns what it did."""
    cfg = site()
    key = cfg["key"]
    max_age = _store.MAX_AGE if max_age is None else max_age
    t0 = time.time()
    s = _store.load_site(key)
    need_site = not s or time.time() - s.get("at", 0) > max_age
    opened_first = 0
    if need_site:
        # open as many windows as the visits will need right away, read the site in one of them, keep them all
        cfg2, mine, how0 = _session(_windows_for(len(refs) if refs else 3, windows))
        opened_first = how0["opened"]
        try:
            _store.save_site(key, evaluate_t(_bridge_of(mine[0]["tab"]), "async () => {" + _SITE + "}", None, ms=40000))
        finally:
            _release(mine)
        s, need_site = _store.load_site(key), False
    if every:
        ids = [p["id"] for p in s["pages"] if not p.get("draft") and not p.get("utility")]
    else:
        ids = [_store.resolve_page(key, r) for r in refs] if refs else _store.stored_pages(key)
    stale = [p for p in ids if (_store.page_age(key, p) is None) or _store.page_age(key, p) > max_age]
    res, how = {}, {"opened": 0, "reused": 0, "windows": 0}
    if stale:
        res, how = _visits([{"page": p} for p in stale], windows, read=True,
                           on_page=lambda pid, r: "error" not in r and _save_page(key, pid, r))
    errors = {p: r["error"] for p, r in res.items() if "error" in r}
    # windows opened for the site read a moment ago were opened by this command, not "reused"
    how = dict(how, opened=how["opened"] + opened_first, reused=max(0, how["reused"] - opened_first))
    return {"site": key, "domains": _domains(s), "asked": len(ids), "fresh": len(ids) - len(stale), "read": len(res) - len(errors),
            "remaining": len([p for p in stale if p not in res]), "errors": errors, "secs": round(time.time() - t0, 1),
            "per_page_s": round(sum(r.get("secs", 0) for r in res.values()) / max(len(res), 1), 1), **how}


def _apply(key, task, writes_by_page, check, verify, windows):
    specs = [{"page": p, "writes": ws, "verify": verify} for p, ws in writes_by_page.items()]
    rows = []

    def on_page(pid, r):
        if "error" not in r and r.get("snap"):
            _save_page(key, pid, r)  # the visit read the page again after writing: the store is exact, not stale

    res, how = _visits(specs, windows, read=True, check_js=check, on_page=on_page)
    for pid, ws in writes_by_page.items():
        r = res.get(pid)
        checks = {c["element"]: c for c in (r or {}).get("checks") or []}
        for i, w in enumerate(ws):
            got = (r.get("writes") or [{}] * len(ws))[i] if r and "error" not in r else {}
            c = checks.get(w["element"])
            rows.append({"page": pid, "element": w["element"], "component": w.get("component"), "prop": w.get("prop"),
                         "kind": w["kind"], "propId": w.get("propId"), "value": w["value"], "old": got.get("before", w.get("old")),
                         "old_override": got.get("before_override"), "after": got.get("after"), "after_override": got.get("after_override"),
                         "stale": got.get("stale"), "ok": bool(got.get("ok")), "render": None if not c else c["pass"],
                         "render_value": None if not c else c.get("value"),
                         "error": (r or {}).get("error") or got.get("error") or (c or {}).get("error") or (None if r else "page not visited (budget): run again")})
    return rows, how


def op_set(resolved, windows=None):
    """Apply a plan resolved by store.resolve_plan: one visit per page checks the old values against the store, writes,
    reads back and checks the render. Records the task. Returns the rows and a summary."""
    key = site()["key"]
    t0 = time.time()
    rows, how = _apply(key, resolved["task"], resolved["writes"], resolved.get("check"), False, windows)
    task = _store.load_task(key, resolved["task"])
    task["writes"] = [w for w in task["writes"] if (w["page"], w["element"], w.get("propId")) not in
                      {(r["page"], r["element"], r.get("propId")) for r in rows}] + [dict(r, at=time.time()) for r in rows]
    task["check"], task["qa"] = resolved.get("check"), None
    _store.save_task(key, resolved["task"], task)
    bad = [r for r in rows if not r["ok"] or r["render"] is False]
    _log(f"designer: wf set '{resolved['task']}': {len(rows)} writes on {len(resolved['writes'])} pages, {len(bad)} not ok")
    return {"task": resolved["task"], "rows": rows, "written": len(rows) - len([r for r in rows if not r["ok"]]), "failed": len([r for r in rows if not r["ok"]]),
            "render_failed": len([r for r in rows if r["render"] is False]), "stale": len([r for r in rows if r["stale"]]),
            "secs": round(time.time() - t0, 1), **how}


def op_qa(task_name=None, windows=None, check=None):
    """End-of-task check, one visit per changed page: the Designer tabs are reloaded first, so what they show was loaded
    from Webflow's servers after the write, then every written value and override flag is read again and the render
    check runs again. Records the result in the task."""
    cfg = site()
    key = cfg["key"]
    task = _store.load_task(key, task_name) if task_name else _store.latest_task(key)
    if not task or not task.get("writes"):
        return {"task": task_name, "error": "no task with writes to check"}
    if check:
        task["check"] = check
    t0 = time.time()
    by = {}
    for w in task["writes"]:
        if w.get("ok"):
            by.setdefault(w["page"], []).append({"element": w["element"], "kind": w["kind"], "propId": w.get("propId"),
                                                 "prop": w.get("prop"), "component": w.get("component"), "value": w["value"]})
    # reload the kept windows that will be used: a reloaded Designer shows what the server has
    n = _windows_for(len(by), windows)
    kept = [w for w in _pool().get(key, []) if w["job"] is None and _pool_ok(w)][:n]
    for w in kept:
        w["tab"].reload(wait_until="commit", timeout=90000)
    if kept:
        _wait_designers([w["tab"] for w in kept])
    rows, how = _apply(key, task["task"], by, task.get("check"), True, n)
    bad = [r for r in rows if not r["ok"] or r["render"] is False]
    task["qa"] = {"at": time.time(), "passed": not bad, "checked": len(rows), "failed": len(bad),
                  "rows": [{k: r[k] for k in ("page", "element", "prop", "value", "after", "after_override", "ok", "render", "error")} for r in rows]}
    _store.save_task(key, task["task"], task)
    _log(f"designer: wf qa '{task['task']}': {len(rows)} checked after a reload, {len(bad)} failed")
    return {"task": task["task"], "passed": not bad, "rows": rows, "checked": len(rows), "failed": len(bad),
            "reloaded": len(kept), "secs": round(time.time() - t0, 1), **how}


def op_read(page_ids, body, arg=None, windows=None):
    """Run a read-only JS body on each page (`a` = arg, `els` = the page's elements); returns {page: value | error}.
    For the rare question the stored data can't answer."""
    res, how = _visits([{"page": p, "body": body, "arg": arg} for p in page_ids], windows, read=False)
    return {"pages": {p: (r.get("v") if "error" not in r else {"error": r["error"]}) for p, r in res.items()}, **how}
