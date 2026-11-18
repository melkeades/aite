"""The site store: everything collected from a Webflow site, on disk, so it is read once and reused.

    data/<site>/site.json            pages (id, name, slug, path, kind, draft) and components (id, name, prop definitions)
    data/<site>/pages/<page id>.json one page as the Designer has it: component instances with every prop, text elements,
                                     and what the canvas renders (which instance renders which text, clamps, cuts)
    data/<site>/tasks/<task>.json    what a task wrote and how its QA went
    data/<site>/live/<label>.json    results of live.py runs

No browser in this file. wf.py uses it for the offline commands (find, plan); ops.py (inside the driver) writes it."""
import json, os, re, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MAX_AGE = 3600  # seconds a page's data counts as fresh (a write through wf.py refreshes it; a hand edit does not)


def _dir(site, *parts):
    d = os.path.join(DATA, site, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, obj):
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)  # atomic: two agents can sync at once


# ---- site ----

def site_path(site):
    return os.path.join(_dir(site), "site.json")


def load_site(site):
    p = site_path(site)
    return _read(p) if os.path.exists(p) else None


def save_site(site, obj):
    _write(site_path(site), dict(obj, at=time.time()))


# ---- pages ----

def page_path(site, page_id):
    return os.path.join(_dir(site, "pages"), page_id + ".json")


def load_page(site, page_id):
    p = page_path(site, page_id)
    return _read(p) if os.path.exists(p) else None


def save_page(site, page_id, obj):
    _write(page_path(site, page_id), dict(obj, at=time.time()))


def page_age(site, page_id):
    p = page_path(site, page_id)
    return time.time() - os.path.getmtime(p) if os.path.exists(p) else None


def stored_pages(site):
    d = _dir(site, "pages")
    return [f[:-5] for f in os.listdir(d) if f.endswith(".json")]


def cms_base(page):
    """The URL folder of a collection template page: its slug is "detail_<collection slug>". (Its publish path is
    not stable: the Designer reports the path of whichever item the template currently shows.)"""
    slug = page.get("slug") or ""
    return "/" + (slug[len("detail_"):] if slug.startswith("detail_") else slug)


def resolve_page(site, ref):
    """A page id from an id, a publish path (/blog), a slug or a name. For a path that belongs to a collection item
    (/blog/some-post) the collection's template page. Raises ValueError when nothing or several match."""
    s = load_site(site)
    if not s:
        raise ValueError(f"no data for {site}: run `wf.py sync {site}` first")
    pages = s["pages"]
    by_id = {p["id"]: p for p in pages}
    if ref in by_id:
        return ref
    path = "/" + ref.strip("/") if ref.startswith("/") or "/" in ref else None
    if path is not None:
        path = path.rstrip("/") or "/"
        hit = [p for p in pages if p.get("kind") != "cms" and ((p.get("path") or "").rstrip("/") or "/") == path]
        if not hit:  # a collection item's URL, or the collection's own path: that collection's template page
            hit = [p for p in pages if p.get("kind") == "cms" and (path == cms_base(p) or path.startswith(cms_base(p) + "/"))]
    else:
        hit = [p for p in pages if ref in (p.get("slug"), p.get("name"))]
    if len(hit) != 1:
        raise ValueError(f"page {ref!r}: {len(hit)} matches" + (": " + ", ".join(f"{p['id']} {p.get('name')!r} {p.get('path')}" for p in hit[:5]) if hit else ""))
    return hit[0]["id"]


def page_label(site, page_id):
    s = load_site(site) or {"pages": []}
    p = next((p for p in s["pages"] if p["id"] == page_id), None)
    if not p:
        return page_id
    # a collection template has no path of its own: the Designer reports the item its canvas happens to show
    return f"{p.get('name')} ({cms_base(p)}/<item>, CMS template)" if p.get("kind") == "cms" else f"{p.get('name')} ({p.get('path')})"


def props_matching(site, page_id, element, needle):
    """The props of one instance whose name contains needle: [{propId, name, type, value, hasOverride}]."""
    i = instance(site, page_id, element)
    return [p for p in (i or {}).get("props", []) if _norm(needle) in _norm(p.get("name"))]


def top_of(site, page_id, element):
    """The top component an instance belongs to: itself when it sits in no other instance (the page shell doesn't
    count), else the outermost instance around it. Returns the instance dict, or None."""
    d = load_page(site, page_id)
    if not d:
        return None
    by = {i["element"]: i for i in d["instances"]}
    i = by.get(element)
    if not i:
        return None
    outer = [x for x in i["inside"] if not by.get(x["element"], {}).get("wraps_page")]
    return by.get(outer[0]["element"]) if outer else i


# ---- queries (offline) ----

def _val(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def find(site, text=None, prop=None, component=None, pages=None, top=None):
    """Search the stored pages. Any mix of:
      text       in a prop value, a text element, or anything the canvas renders (so CMS-bound text is found too)
      prop       a prop whose name contains this
      component  instances of the component whose name contains this (exact match wins)
      pages      limit to these page ids
      top        True: only instances that sit in no other instance except a page-level wrapper
    Returns rows: {page, page_name, element, component, inside, top, why, prop?, propId?, value?, hasOverride?}."""
    rows = []
    for pid in (pages or stored_pages(site)):
        d = load_page(site, pid)
        if not d:
            continue
        wrappers = {i["element"] for i in d["instances"] if i.get("wraps_page")}
        inst_by = {i["element"]: i for i in d["instances"]}
        rendered = {}
        for r in d.get("rendered") or []:
            rendered.setdefault(r["instance"], []).append(r)
        names = [i["name"] for i in d["instances"]]
        exact = component and any(_norm(n) == _norm(component) for n in names)
        for i in d["instances"]:
            inside = [x for x in i["inside"] if x["element"] not in wrappers]
            is_top = not inside and not i.get("wraps_page")
            if top is not None and is_top != top:
                continue
            if component and not ((_norm(i["name"]) == _norm(component)) if exact else (_norm(component) in _norm(i["name"]))):
                continue
            base = {"page": pid, "page_name": d["page"].get("name"), "path": d["page"].get("path"), "element": i["element"],
                    "component": i["name"], "inside": [x["name"] for x in inside], "top": is_top}
            hits = []
            for p in i["props"]:
                name_hit = prop and _norm(prop) in _norm(p.get("name"))
                text_hit = text and _norm(text) in _norm(_val(p.get("value")))
                if (prop and not name_hit) or (text and not prop and not text_hit):
                    continue
                if prop or text:
                    hits.append(dict(base, why="prop name" if name_hit else "prop value", prop=p.get("name"), propId=p["propId"],
                                     type=p.get("type"), value=p.get("value"), hasOverride=p.get("hasOverride"), default=p.get("defaultValue")))
            if text and not prop:
                for r in rendered.get(i["element"], []):
                    if _norm(text) in _norm(r["text"]):
                        hits.append(dict(base, why="rendered text", value=r["text"], cls=r.get("cls"), clamp=r.get("clamp"), cut=r.get("cut")))
            if not prop and not text:
                hits.append(dict(base, why="component"))
            rows += hits
        if text and not prop and not component and top is None:  # text rendered by a plain element, outside every component
            for r in rendered.get(None, []):
                if _norm(text) in _norm(r["text"]):
                    rows.append({"page": pid, "page_name": d["page"].get("name"), "path": d["page"].get("path"), "element": r.get("root"),
                                 "component": None, "inside": [], "top": False, "why": "rendered text", "value": r["text"],
                                 "cls": r.get("cls"), "clamp": r.get("clamp"), "cut": r.get("cut")})
    return rows


def locate(site, records):
    """Which component instance renders each finding. records: dicts with `text` and a page (`page`, `path`, `url` or
    `designer_page_id`), optionally `classes`. Tried in order on that page: the text in what the canvas renders, the
    text in a prop value, then (for CMS templates, whose canvas shows other items than the live page) a rendered node
    with the same class that is clamped. Returns one row per record: {i, page, element, component, inside, top, by} or
    by = "not found" / "no component"."""
    out = []
    for i, rec in enumerate(records):
        ref = rec.get("page") or rec.get("designer_page_id") or rec.get("path") or rec.get("url") or ""
        if "//" in ref:
            ref = "/" + ref.split("//", 1)[1].split("/", 1)[1] if "/" in ref.split("//", 1)[1] else "/"
        row = {"i": i, "text": (rec.get("text") or "")[:60], "ref": ref}
        try:
            pid = resolve_page(site, ref)
        except ValueError as e:
            out.append(dict(row, by="not found", error=str(e)))
            continue
        row["page"] = pid
        d = load_page(site, pid)
        if not d:
            out.append(dict(row, by="not found", error=f"page not in the store: wf.py sync {site} {pid}"))
            continue
        needle = _norm(rec.get("text"))[:40]
        hits = [r for r in find(site, text=needle, pages=[pid])] if needle else []
        by = "text"
        classes = rec.get("classes") or rec.get("cls") or []
        classes = classes if isinstance(classes, list) else str(classes).split(".")
        last = [c for c in classes if c and not c.startswith("w-variant")][-1:] or [None]
        if last[0]:  # the finding names its class: a text hit in a node of another class is another rendering of the text
            same = [h for h in hits if h.get("why") != "rendered text" or last[0] in (h.get("cls") or "").split(".")]
            hits = same if same or not any(h.get("why") == "rendered text" for h in hits) else []
        if not hits:
            cand = [r for r in d.get("rendered") or [] if last[0] and last[0] in (r.get("cls") or "").split(".") and r.get("clamp")]
            insts = {r["instance"] for r in cand}
            if len(insts) == 1:
                el = insts.pop()
                hits = [h for h in find(site, pages=[pid]) if h["element"] == el] if el else [{"element": cand[0].get("root"), "component": None, "inside": [], "top": False}]
                by = "class"
        if not hits:
            out.append(dict(row, by="not found"))
            continue
        # the same text can be rendered by several instances (a card and a list of the same posts): prefer a clamped one
        hits.sort(key=lambda h: (not h.get("cut"), not h.get("clamp"), h.get("why") != "rendered text"))
        h = hits[0]
        out.append(dict(row, element=h["element"], component=h["component"], inside=h.get("inside") or [], top=h.get("top"),
                        by=by if h["component"] else "no component", others=sorted({x["component"] for x in hits[1:] if x["component"] and x["element"] != h["element"]})))
    return out


def instance(site, page_id, element):
    d = load_page(site, page_id)
    return next((i for i in (d or {}).get("instances", []) if i["element"] == element), None)


# ---- plans: what to write, resolved against the store before any browser is touched ----

def resolve_plan(site, plan):
    """Turn a plan into concrete writes per page, checking every id, prop name and value type against the store.
    A plan: {"task": "name", "set": [ITEM, ...], "check": "<js: el => value>" (optional)}. ITEM is one of
      {"where": {"component": "Features-6", "pages": [ids or paths], "top": true, "text": "..."}, "props": {"Prop name or id": value}}
      {"page": id or path, "element": id, "props": {...}}
      {"page": id or path, "element": id, "text": "new text"}          (a text element)
    Returns {"task", "writes": {page_id: [{"element", "component", "kind", "propId"?, "prop"?, "value", "old", "old_override"}]},
    "errors": [...]}. Nothing is written when there are errors."""
    out, errors = {}, []
    for n, item in enumerate(plan.get("set") or []):
        try:
            targets = []
            unknown = {k for k in item if not k.startswith("_")} - {"where", "page", "element", "props", "text"}
            if unknown:
                raise ValueError(f"unknown key(s) {sorted(unknown)}; an item has where or page+element, and props or text")
            if "where" in item:
                w = item["where"]
                unknown = set(w) - {"component", "pages", "top", "text", "element"}
                if unknown:
                    raise ValueError(f"unknown where key(s) {sorted(unknown)}; allowed: component, pages, top, text, element")
                pages = [resolve_page(site, p) for p in w["pages"]] if w.get("pages") else None
                rows = find(site, component=w.get("component"), pages=pages, top=w.get("top"), text=w.get("text"))
                seen = set()
                for r in rows:
                    if w.get("element") and r["element"] != w["element"]:
                        continue
                    if (r["page"], r["element"]) not in seen:
                        seen.add((r["page"], r["element"]))
                        targets.append((r["page"], r["element"]))
                if not targets:
                    raise ValueError("matches no instance in the stored pages (sync those pages first?)")
            else:
                targets.append((resolve_page(site, item["page"]), item["element"]))
            for pid, el in targets:
                d = load_page(site, pid)
                if not d:
                    raise ValueError(f"page {pid} is not in the store: wf.py sync {site} {pid}")
                if "text" in item:
                    t = next((s for s in d.get("strings") or [] if s["element"] == el), None)
                    if not t:
                        raise ValueError(f"no text element {el} on {page_label(site, pid)}")
                    out.setdefault(pid, []).append({"element": el, "kind": "text", "value": item["text"], "old": t["text"]})
                    continue
                inst = next((i for i in d["instances"] if i["element"] == el), None)
                if not inst:
                    raise ValueError(f"no component instance {el} on {page_label(site, pid)}")
                for key, value in (item.get("props") or {}).items():
                    cands = [p for p in inst["props"] if p["propId"] == key] or [p for p in inst["props"] if _norm(p.get("name")) == _norm(key)]
                    if len(cands) != 1:
                        near = [p.get("name") for p in inst["props"] if _norm(key).split(" ")[-1] in _norm(p.get("name"))][:6]
                        raise ValueError(f"{inst['name']} on {page_label(site, pid)} has {len(cands)} props named {key!r}"
                                         + (f"; similar: {near}" if near else "") + ("; use the prop id" if cands else ""))
                    p = cands[0]
                    if value is not None and p.get("type") == "boolean" and not isinstance(value, bool):
                        raise ValueError(f"{key!r} is boolean, got {value!r}")
                    if value is not None and p.get("type") in ("textContent", "string") and not isinstance(value, str):
                        raise ValueError(f"{key!r} is text, got {value!r}")
                    out.setdefault(pid, []).append({"element": el, "component": inst["name"], "kind": "prop", "propId": p["propId"],
                                                    "prop": p.get("name"), "value": value, "old": p.get("value"),
                                                    "old_override": p.get("hasOverride")})
        except (ValueError, KeyError) as e:
            errors.append(f"set[{n}]: {e}")
    return {"task": plan.get("task") or "task", "writes": out, "errors": errors, "check": plan.get("check")}


# ---- task ledger ----

def task_path(site, task):
    return os.path.join(_dir(site, "tasks"), re.sub(r"[^A-Za-z0-9._-]+", "-", task) + ".json")


def load_task(site, task):
    p = task_path(site, task)
    return _read(p) if os.path.exists(p) else {"task": task, "writes": [], "qa": None}


def save_task(site, task, obj):
    _write(task_path(site, task), obj)


def latest_task(site):
    d = _dir(site, "tasks")
    fs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
    return _read(max(fs, key=os.path.getmtime)) if fs else None
