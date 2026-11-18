"""store.py: page lookup, offline queries and plan resolution, against a made-up site. No browser, runs in a second.

    python tests/test_store.py"""
import os, sys, tempfile

PW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PW)
import store  # noqa: E402

store.DATA = tempfile.mkdtemp(prefix="store-test-")
S, results = "demo", []


def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(info)[:300]}]" if info and not ok else ""))


def prop(pid, name, typ, value, over=False, default=None):
    return {"propId": pid, "name": name, "type": typ, "value": value, "hasOverride": over, "defaultValue": default}


store.save_site(S, {"pages": [
    {"id": "p1", "name": "Pricing", "slug": "pricing", "path": "/pricing", "kind": "static"},
    {"id": "p2", "name": "Blog", "slug": "blog", "path": "/blog", "kind": "static"},
    {"id": "t1", "name": "Blogs Template", "slug": "detail_blog", "path": "/blog/some-item-the-designer-shows", "kind": "cms"},
    {"id": "h", "name": "Home", "slug": "", "path": "/", "kind": "static"}], "components": []})
store.save_page(S, "p1", {"page": {"id": "p1", "name": "Pricing", "path": "/pricing"}, "elements": 9, "strings": [{"element": "s1", "text": "Old text"}],
    "instances": [
        {"element": "w", "name": "page-wrapper", "inside": [], "wraps_page": True, "props": []},
        {"element": "a", "name": "Features-6", "inside": [{"element": "w", "name": "page-wrapper"}], "props": [
            prop("c1", "Disable line clamp", "boolean", False, default=False), prop("t1", "Title", "textContent", "Related services", True),
            prop("v1", "Variant", "variant", "x"), prop("v2", "Variant", "variant", "y")]},
        {"element": "b", "name": "Features-6", "inside": [{"element": "w", "name": "page-wrapper"}], "props": [
            prop("c1", "Disable line clamp", "boolean", False, default=False), prop("t1", "Title", "textContent", "Costs", True)]},
        {"element": "g", "name": "Grid", "inside": [{"element": "w", "name": "page-wrapper"}], "props": []},
        {"element": "c", "name": "Card", "inside": [{"element": "w", "name": "page-wrapper"}, {"element": "g", "name": "Grid"}], "props": [
            prop("c9", "Disable line clamp", "boolean", False, default=False)]},
        {"element": "f3", "name": "Features-3", "inside": [{"element": "w", "name": "page-wrapper"}], "props": []}],
    "rendered": [{"instance": "c", "text": "A CMS card title that is cut", "cls": "card__h", "clamp": "2", "cut": True},
                 {"instance": None, "root": "x9", "text": "Plain element text", "cls": "text-style-2lines", "clamp": "2", "cut": True}]})

# page lookup
check("page by id", store.resolve_page(S, "p1") == "p1")
check("page by path", store.resolve_page(S, "/pricing") == "p1" and store.resolve_page(S, "/pricing/") == "p1")
check("home by /", store.resolve_page(S, "/") == "h")
check("static page wins over the collection of the same path", store.resolve_page(S, "/blog") == "p2")
check("a collection item's URL gives the template", store.resolve_page(S, "/blog/any-post") == "t1")
check("page by name", store.resolve_page(S, "Blogs Template") == "t1")
try:
    store.resolve_page(S, "/nope")
    check("unknown page raises", False)
except ValueError:
    check("unknown page raises", True)

# queries
r = store.find(S, prop="clamp")
check("find by prop name: 3 instances", len(r) == 3 and {x["element"] for x in r} == {"a", "b", "c"}, r)
check("top: the page shell doesn't count as a parent", [x["top"] for x in sorted(r, key=lambda x: x["element"])] == [True, True, False], r)
check("find --top leaves the nested one out", {x["element"] for x in store.find(S, prop="clamp", top=True)} == {"a", "b"})
check("component: exact name wins over substring", {x["element"] for x in store.find(S, component="Features-6")} == {"a", "b"})
r = store.find(S, text="cms card title")
check("rendered text finds the instance of CMS-bound text", len(r) == 1 and r[0]["element"] == "c" and r[0]["inside"] == ["Grid"] and r[0]["cut"], r)
r = store.find(S, text="Plain element")
check("text outside every component is reported as such", len(r) == 1 and r[0]["component"] is None, r)
check("text in a prop value", store.find(S, text="related services")[0]["element"] == "a")

# plans
p = store.resolve_plan(S, {"task": "t", "set": [{"where": {"component": "Features-6", "pages": ["/pricing"], "text": "Related services"}, "props": {"Disable line clamp": True}}]})
check("where + text selects one instance", not p["errors"] and p["writes"] == {"p1": [{"element": "a", "component": "Features-6", "kind": "prop", "propId": "c1", "prop": "Disable line clamp", "value": True, "old": False, "old_override": False}]}, p)
p = store.resolve_plan(S, {"set": [{"where": {"component": "Features-6"}, "props": {"disable LINE clamp": True}}]})
check("where without text selects both; prop names are case-insensitive", sum(len(v) for v in p["writes"].values()) == 2 and not p["errors"], p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "a", "props": {"Variant": "z"}}]})
check("an ambiguous prop name is an error, nothing resolved", p["errors"] and "2 props named" in p["errors"][0] and not p["writes"], p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "a", "props": {"v2": "z"}}]})
check("a prop id resolves the ambiguity", not p["errors"] and p["writes"]["p1"][0]["propId"] == "v2", p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "a", "props": {"Disable line clamp": "yes"}}]})
check("a wrong value type is an error", p["errors"] and "boolean" in p["errors"][0], p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "a", "props": {"Disable line clamp": None}}]})
check("null (remove the override) is allowed", not p["errors"] and p["writes"]["p1"][0]["value"] is None, p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "zzz", "props": {"Title": "x"}}]})
check("an unknown element is an error", p["errors"] and "no component instance" in p["errors"][0], p)
p = store.resolve_plan(S, {"set": [{"page": "/pricing", "element": "s1", "text": "New"}]})
check("a text write keeps the old text", not p["errors"] and p["writes"]["p1"][0] == {"element": "s1", "kind": "text", "value": "New", "old": "Old text"}, p)
p = store.resolve_plan(S, {"set": [{"page": "/blog", "element": "a", "props": {"Title": "x"}}]})
check("a page that isn't in the store is an error that says to sync", p["errors"] and "sync" in p["errors"][0], p)
p = store.resolve_plan(S, {"set": [{"where": {"component": "Nope"}, "props": {"Title": "x"}}]})
check("a where that matches nothing is an error", p["errors"] and "matches no instance" in p["errors"][0], p)

bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)} of {len(results)} passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
