# Playwright driver for Webflow Designer

A headed Chrome (CDP `:9222`, logged-in profile) and a long-lived Python driver that runs snippets you POST to it. Site-agnostic: keep site-specific notes in that project's AGENTS.md.

| File | Purpose |
|---|---|
| `driver.py` | Attaches to Chrome and serves `POST http://127.0.0.1:8765/exec` and `GET /status`. Globals (`page`, `context`, `browser`, your own vars) persist between snippets; set `result = ...` to return a value. Env overrides: `DRIVER_PORT`, `CDP_PORT`, `DRIVER_STOP`, `DRIVER_TOKEN_FILE`. |
| `x.py` | `python x.py [--budget S] [--label L] snippet.py` (or stdin) runs a snippet. `python x.py --status` shows the running and last snippet. Exit codes: 0 ok, 1 snippet error, 3 stack down, 4 over budget, 5 busy. |
| `watchdog.py` | `python watchdog.py 50` samples the RTX 4090 per process (`nvidia-smi pmon`). It kills the automation Chrome only when *its own* processes decode video or exceed N% SM for 3 samples in a row; other apps' load is only logged to `gpu.log`. A kill writes `STOP`; delete it to resume. `--test-kill` simulates one. |
| `bridge.py` | Webflow helpers loaded into every snippet (reloaded when the file changes): `open_site`, `use`, `site`, `js`, `until`, `switch_page`, `open_canvas`, `select`, `designer`, `canvas`, `live`, `clip`, `paste_at`, `launch_bridge`, `relaunch_bridge`. See "Webflow helpers" and "Sites". |
| `sites/` | Per-site local settings, `<key>.json` (git-ignored; `example.json` is the template). Written by `open_site`. |
| `tests/stress_guardrails.py` | Scores the guardrails against ground truth (see Guardrails). |
| `chrome-profile/` | Logged-in Webflow profile (git-ignored). Reuse it; log in once per machine. |

## Start

Check first: `python x.py --status` (exit 3 means the driver or Chrome is not up).

Run from this folder. On a new machine `chrome-profile/` doesn't exist yet: Chrome creates it, then log in to Webflow once in that window. It is git-ignored (cookies, logins, tokens). The adapter LUID is per machine: find yours or drop `--use-angle`/`--use-adapter-luid`.

```powershell
# 1. Chrome (headed, pinned to the 5090; LUID 0x166d5 on this machine)
Start-Process chrome.exe "--user-data-dir=`"$PWD\chrome-profile`" --remote-debugging-port=9222 --no-first-run --window-size=1600,1000 --use-angle=d3d11 --use-adapter-luid=0,166d5 --disable-accelerated-video-decode --autoplay-policy=user-gesture-required about:blank"
# 2. Watchdog, then driver
Start-Process -WindowStyle Hidden python 'watchdog.py 50'
Start-Process -WindowStyle Hidden python driver.py
```

## Guardrails

Enforced by `driver.py` and `x.py`.

- **Budget:** `--budget S` (default 90 s, max 300). The reply always arrives by budget + 10 s. Over budget exits 4: either still running (the driver stays busy until it ends) or finished late (result kept; the snippet doesn't call `check()` often enough).
- **No queue:** a request while another runs is refused with exit 5, naming the running snippet, its elapsed time and last progress.
- **Snippet helpers:**
  - `run_items(items, fn, out, key=..., per_item=20)` runs `fn` per item, skips items already in `out` (errored ones are retried), saves after each, and stops before the budget. Returns `done_total`, `remaining`, `avg_s`, `slow` (over `per_item` s) and `errors`. Call again until `remaining == 0`.
  - `evaluate_t(target, js, arg, ms=20000)` rejects with `ITEM TIMEOUT` after `ms`.
  - `progress(msg)` is shown by `--status` and in BUSY/over-budget replies. `check()` raises `Budget` when time is up.
- **Timings:** every reply prints `[secs of budget]`; use it and `avg_s` for estimates.
- **Access:** the driver runs arbitrary code, so every request needs the `X-Token` from `.driver-token-<port>` (new random token per start, git-ignored; `x.py` sends it). Requests with an `Origin` header (any web page) or a non-localhost `Host` (DNS rebinding) get 403; `x.py` exits 1 with `FORBIDDEN: <reason>`. CDP `:9222` has no such check: any local process can drive the logged-in Chrome while it runs.

```python
# one bounded batch; re-run until remaining == 0
JS = open(r"...\walk.js").read()
result = run_items(PAGES, lambda p: evaluate_t(app, JS, {"id": p}), OUT, per_item=20)
```

**Stress test:** `python tests/stress_guardrails.py` (about 2.5 min; `--auth-only` runs just the access checks in seconds, without Chrome). It runs its own drivers on 8766 (real Chrome) and 8767 (dead CDP port) with its own STOP file, so 8765 is untouched, and prints TP/FP/TN/FN per detector against each snippet's real start and end. Rerun after any change to `driver.py` or `x.py`; it must show 0 FP and 0 FN. It covers over budget (±30 ms around the budget), the reply bound, busy under contention (290 requests), the `run_items` slow flag and exactly-once, `evaluate_t` (±40 ms), stack down, `--status`, and access (missing/wrong/prefix token, browser simple POST, `Origin`, rebinding `Host`, bad `Content-Length`; with Chrome it also confirms refused snippets never ran). Not covered: a real watchdog kill or Chrome restart.

## Webflow helpers (`bridge.py`)

Available in every snippet without importing; use them instead of rewriting frame lookups and polls.

```python
open_site("https://my-site.design.webflow.com/")     # open/reuse its Designer, launch the bridge, save sites/my-site.json
r = js("const h = await byId(a); await tm('rename', () => h.setDisplayName('x')); return await h.getDisplayName()", el_id)
# r = {ok, v | err, ms (in frame), rt (round trip), T: {rename: 47}, E: {...}}
switch_page("Home"); open_canvas(component_id)        # page by id, name or slug; fire, poll until current, settle
until("return (await webflow.getSelectedElement())?.type === 'Heading'")   # -> (seconds, value)
select(el_id); paste_at(x, y, "text")                  # UI: select, then click a field, Ctrl+A, paste, Enter
canvas(); live("/pricing")                             # the canvas frame; a tab on the staging URL (cache-busted)
emulate(t, 390); t.reload(); snap(t, path, full=True)   # exact layout width via CDP, full-page/clip shot via CDP
```

- Inside a `js()` body: `a` is the argument, `byId(id)` finds an element on the current page or canvas, and `tm(label, fn, ms=8000)` times one call and records errors or hangs without stopping the batch.
- `js()` never raises for JS errors or timeouts (`ok: False`). After a whole-call timeout it still returns the `T`/`E` gathered so far.
- Some invalid bridge calls never settle (e.g. `setSettings('link', 1)` on a Heading); keep `tm`'s per-call `ms` short.
- The driver's `page` is just the first tab. Use `designer()`, `canvas()`, `bridge_frame()` and `live()`, which resolve the current site's tabs.

## Sites

Nothing in the pipeline is tied to one Webflow project. Each site has local settings in `sites/<key>.json` (git-ignored; `sites/example.json` is the template):

```json
{"shortName": "my-site", "name": "My Site", "siteId": "…", "designer": "https://my-site.design.webflow.com/",
 "staging": "https://my-site.webflow.io/", "notes": "anything site-specific"}
```

- **Pick the site per call:** `x.py --site <ref>` (header `X-Site`). `<ref>` can be:
  - the file key, short name, site name or site id
  - its Designer or staging URL; an unknown site's URL creates `sites/<short>.json`, so a new project needs no setup
  The site lasts for that one snippet; the next snippet starts with none, so sessions sharing the driver never inherit each other's site.
- **Inside a snippet:**
  - `use(ref)` switches the site for the rest of that snippet.
  - `open_site(ref)` opens or reuses the Designer tab, launches the bridge, checks `webflow.getSiteInfo()` matches, and records `siteId` and `name`.
  - `site()` returns the settings. `save_site(key, **fields)` adds your own (e.g. `notes`, key page ids).
- **Without a site:** the helpers work only while exactly one Designer tab is open. With several they raise `SEVERAL DESIGNERS OPEN (a, b)` instead of guessing, because a guess edits another project.
- **Bridge:** `launch_bridge()` opens it from the Apps panel by its label (no coordinates) and minimizes it. `relaunch_bridge()` reloads the Designer first; it's needed after a driver restart. `bridge_frame()` skips the detached copies a reload leaves behind.
- **One client only:** a second Playwright driver on the same Chrome sees the Designer's cross-origin frames (canvas, bridge) as blank. Test helpers on the production driver; the stress test only covers the driver itself.

## Playwright pitfalls

- **GPU:** a headed window renders on the 4090 by default, even with `--disable-gpu`. Pin it with `--use-angle=d3d11 --use-adapter-luid=0,166d5`. `--disable-accelerated-video-decode` keeps stray video off the GPU decoders, and `driver.py` blocks video URLs in Webflow Designer tabs (`*.design.webflow.com`, whose canvas plays every video on the page) through CDP `Network.setBlockedURLs`. Other tabs, e.g. a staging page, play video normally. Open the Designer through `open_site()` / `designer()` so the tab is blocked before it navigates.
- **Local network prompt:** pages that load a LAN dev server (a Vite origin like `https://192.168.x.x:5173`) stop on Chrome's "Access other devices on your local network" prompt. On connect, `driver.py` grants `local-network-access` to every origin for that Chrome session (CDP `Browser.setPermission`, no origin), so no click is needed.
- **Stack down:** when `STOP` exists or Chrome is gone, the driver refuses snippets and `x.py` exits 3 with the reason. After Chrome restarts and `STOP` is deleted, the next call reconnects.
- **Viewport:** `page.set_viewport_size` can silently stop applying (after `bring_to_front`, `viewport_size` reports 390 while the page lays out at 1440). For layout checks use `emulate(tab, width)`, which sets CDP device metrics and raises if `innerWidth` disagrees; `clear_emulation(tab)` undoes it.
- **Screenshots:** Playwright page and element screenshots can hang 30–100 s on Webflow pages and on the Designer. Use `snap(tab, path, full=True)` or `snap(tab, path, clip={...})` (CDP `Page.captureScreenshot`, JPEG with `quality=`). On the Designer, clip to the panel you need.
- **Headless doesn't work for the Designer** (Webflow rejects the `HeadlessChrome` UA). Never spoof the UA; that is bot evasion.
- **No `context.route()` with the sync driver:** routed requests are only answered while a snippet runs, so an idle driver stalls every tab. Block URLs through CDP.
- **A driver only sees bridge frames created after it attached.** After a driver restart (or from a second client), the bridge iframe is invisible until it is relaunched: run `relaunch_bridge()` with the site set (reloads the Designer, launches the app, minimizes it; about 17 s).
- **One snippet at a time on the main thread.** It can't be interrupted from outside. If one is truly hung, ask the user before killing `driver.py`.
- **Never `await` canvas switches** (`webflow.openCanvas`, `switchPage`). Fire the call, then poll.
- **No `networkidle`** on Webflow. Use `domcontentloaded`, then wait for a specific element.
- **Encoding:** run with `PYTHONIOENCODING=utf-8`, or `\u200d` / `–` in results crash `x.py`.
- **Text input:** paste, never type. For rich text, write `text/html` with `ClipboardItem` after `context.grant_permissions(["clipboard-read","clipboard-write"], origin=...)`. Lone or trailing spaces get trimmed: paste `"day to"`, not `" "`.
- **Never complete bot checks or CAPTCHAs.** Hand them to the user.

## Webflow Designer pitfalls

- **Bridge app:** the Designer API lives in the `webflow-ext.com` iframe (Webflow MCP Bridge App). It disappears after reloads, canvas switches or closing it with X; minimize it instead. After 2 failed relaunches, reload the Designer tab.
- **Coordinates go stale:** panels scroll and popups shift rows. Screenshot before each coordinate click and re-read state after each UI edit.
- **Hidden copies:** the Designer keeps hidden copies of the canvas. Find text with `getBoundingClientRect().width > 0`. The copies don't all update live (one lagged after a slot reset), so compare frames or verify through the API.
- **Binding buttons** in the Settings panel are `[data-automation-id="binding-button_<Label>"]` (e.g. `binding-button_Content` for rich text); click one, then pick the prop by its name.
- **Script selections don't count** in rich text: Webflow's editor ignores a JS `Selection`, and Backspace deletes at the real caret. Select with clicks and keys (Home, Shift+Arrow, Shift+End, Ctrl+A) and check `getSelection()` first.
- **Rich-text props:** **Edit Rich Text**, Ctrl+A, paste the full HTML once, then remove the trailing `\u200d` paragraph (Ctrl+End, Backspace).
- **Slot defaults:** editing slot content on a page detaches it into a per-page copy. Edit the default in the component canvas, then `slot.resetToDefault()` on the pages.
- **Component vs instance:** list every instance and compare values before editing. Edit the component only if the change is right for all of them.
- **Links:** internal links in components and instances use type `page` (`{mode:'page', to:{pageId}}`), never `url`. Component link props can't point at "this item's referenced item"; only fixed items are offered.
- **Collection-list filters** set in the Designer (e.g. "Item is not Current Item") show as `filters: []` in the Data API; verify them in the UI. "Current item" filters aren't offered inside components; unlink the instance if one is needed.
- **Select options and form-state text** aren't in any API: Settings → Choices (pencil → Text/Value). Success messages show after switching State to Success.

## Data API and bridge notes

- `set_component_instance_prop_values`: CMS binding is `type: "bindable"`, `binding_source_type: "cms"`; referenced fields use `refFieldId:::fieldId`. Link modes `page` and `collectionPage` are rejected; use bridge `el.setProps`.
- Collection-item link on an element: `data_element_settings_tool` → `set_settings` → `{key:"link", static_link:{mode:"collectionPage"}}`.
- `el.getProps()` has no prop names; match by `propId`. Link values are `{mode, to}` with mode `page`, `url`, `pageSection`, `email`, `phone` or `collectionPage`.
- `el.searchPropAvailableFields(propId)` lists filter fields and operators (`{filters:[{fieldSlug,operator,value}], filterMatch}`); `el.searchBindableSources()` lists CMS fields and reference paths.
- `getAllElements()` excludes slot contents: walk `el.getSlots()` → `slot.getChildren()`.
- Slot defaults aren't in the Data API. `component.getRootElement()` and `component.getSlotDefault(slotId)` read the trees without opening the canvas, but `getProps()` on instances inside them throws "Element not found" unless that component's canvas is open. `webflow.getSelectedElement()` returns null for slot-default content; edit it in the canvas Properties panel.
- Remove one override: `el.setProps([{propId, value: null}])`, then read back `hasOverride: false`. `resetAllProps()` resets every prop.
- **Discover before guessing:** `el.searchSettings()` lists every settable key with `valueType`, `canBind` and the current value. Always use the object form `el.setSettings({key: value})`; `setSettings('link', v)` fails with "Invalid mode".
- **Bindings** inside a component: `el.setSettings({<key>: {sourceType: 'prop', propId}})`. Rich text uses key `richText`, not `text`. To unbind, set a plain value (`{richText: {innerText}}`); the `{sourceType: 'static', value}` shape `searchSettings` shows is rejected.
- **Prop types** for `component.createProp({type, name})`: `textContent`, `string`, `richText`, `image`, `link`, `video`, `number`, `boolean`, `id`, `altText` (not listed by the API; only the error message names them). `variant` comes from `createVariant`, `slot` from a Slot element. Video values are `{src}`. A `setProps` batch is atomic: one invalid value fails all of it.
- **`removeProp` on a bound prop** silently unbinds the elements (they fall back to empty). Check bindings first.
- **`registerComponent(name, el)`** moves the element into the component and leaves no instance behind; `parent.append(component)` places one. `unlinkComponent()` turns an instance back into elements (its slots become plain Blocks).
- **Slots:** append `elementPresets.Slot` inside the component; that creates a `slot` prop (not renamable with `setProp`). Slot defaults and overrides must be component instances (plain elements are rejected, in the UI too):
  - default: `component.appendToSlotDefault(slotPropId, childComponent)`, no canvas needed; `getSlotDefault(slotPropId)` returns the instances.
  - per instance: `inst.getSlots()[i]` has `append(component)`, `prepend`, `clear`, `getChildren`, `resetToDefault`.
  - an inherited default and a cleared slot both read as `[]` (bridge and Data API). Only the rendered canvas shows the difference.
- **Pages:** set `useTitleAsSearchTitle(false)` (and the matching `useTitleAs…`/`useDescriptionAs…` switches) before `setSearchTitle`/OG setters, or they throw. Page setters take 0.35–1.2 s each, `setDraft` about 4 s.
- `getSelectedVariant()` throws unless the component is entered (canvas open).
- **Undo:** Ctrl+Z in the Designer also undoes bridge writes. Don't send it unless you mean to.
- Verify by reading values back through the API, not with screenshots alone.

## Speed and reliability

- **Time one item, then batch with `run_items`.** Per-page cost varies widely: a full bridge page walk is about 5 s at the median (2.7 s of it the switch), but heavy pages take 70–200 s, so one site-wide call can run 25+ minutes. A component walk is under 1 s.
- **Which layer:** measured on a test page.

  | Layer | Typical cost |
  |---|---|
  | Bridge, in frame | reads 15–30 ms (p95 55), element writes 40–90 ms, page writes 0.35–4 s |
  | Bridge, parallel | `Promise.all` of 50 writes 0.46 s, about 10× faster than sequential |
  | Playwright → bridge round trip | about 45 ms (p95 90) |
  | UI click or paste plus read-back | 0.2–1.8 s |
  | Data API (MCP) call | 6–19 s |
  | Canvas switch | 1.4–3 s; Designer cold load about 60 s |

  So: do bulk reads and writes through the bridge inside one `evaluate`, batched with `Promise.all`; use the Data API for one-offs, and the UI only for what has no API.
- **Each getter is a round trip** (about 20 ms): listing 55 pages with their parents took 2.8 s. Fetch once and reuse the handles.
- **Time ops inside the frame** with a small wrapper that records per-call ms and catches per call, so one failure doesn't abort the batch or lose earlier timings.
- **Agent time dominates:** in one benchmark 72% of the wall clock was the agent writing snippets between calls, not the tools. Reuse a helper file (frame lookup, `js()` wrapper, poll-until) instead of rewriting it per snippet.
- **Switching pages:** fire `webflow.switchPage(page)`, poll `getCurrentPage().id`, then wait about 1.5 s before `getAllElements()`, or you read the previous page.
- **Component canvas:** fire `webflow.openCanvas({componentId})`, poll `getCurrentComponent().id`, then wait about 5 s.
  - Images are lazy: set `img.loading = 'eager'`, scroll each into view, wait, then read `currentSrc` (otherwise SVG placeholders).
  - Read every non-extension frame and keep the one with real image sources.
  - Slot wrappers are `.slot[data-w-id="<slot id>"]`, matching `instance.getSlots()` IDs. A page inherits a slot default only when that slot has 0 children on the page.
  - Links render resolved (e.g. `/contact-form`), so `#` in the canvas is a real `#`.
- **Live site checks:** render in a new tab (`context.new_page()`), block video through CDP before `goto`, scroll to load lazy images, and judge visibility from computed styles. About 6 s per page. A static HTML crawl misjudges CSS-hidden elements and misses JS-built links.
- **Keep big reads out of the conversation.** Data API `get_all_elements` returns 50–100 KB per page; prefer `query_elements` with a filter, or a script that writes to a file and returns counts.
- **Health:** `x.py --status` answers even while a snippet runs (exit 3 if down). Also check that the `watchdog.py` process exists and that there is no `STOP` file.
