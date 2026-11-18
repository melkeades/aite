# Reference: Designer UI work and internals

Not required reading. AGENTS.md and README.md are enough for tasks that go through `wf.py`. Open the section you need when you write a `wf.py run` snippet (Designer UI, canvas, styles, structure) or change the tools.

## Guardrails

Enforced by `driver.py` and `x.py`.

- **Budget:** `--budget S` (default 90 s, max 300). The reply always arrives by budget + 10 s. Over budget exits 4: either still running (the driver stays busy until it ends) or finished late (result kept; the snippet doesn't call `check()` often enough).
- **Stop:** `x.py --stop` makes the running snippet's next `check()` raise (exit 1, `STOPPED`). A snippet that never calls `check()` or a helper that does can't be stopped: ask the user before killing `driver.py`.
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
switch_page(page_id); open_canvas(component_id)       # page by id, name or slug (prefer id; a repeated name raises AMBIGUOUS); fire, poll until current, settle
until("return (await webflow.getSelectedElement())?.type === 'Heading'")   # -> (seconds, value)
select(el_id); paste_at(x, y, "text")                  # UI: select, then click a field, Ctrl+A, paste, Enter
canvas()                                               # the canvas frame
t = live("/pricing")                                   # one staging page in a window of its own, for a look right after a
                                                       # Designer change; raises if it doesn't load. Reused by later calls.
snap(t, path, clip={...}); snap(designer(), path, clip={...})   # CDP capture; on the Designer, clip to the panel you need
```

- Inside a `js()` body: `a` is the argument, `byId(id)` finds an element on the current page or canvas, and `tm(label, fn, ms=8000)` times one call and records errors or hangs without stopping the batch.
- `js()` never raises for JS errors or timeouts (`ok: False`). After a whole-call timeout it still returns the `T`/`E` gathered so far.
- Some invalid bridge calls never settle (e.g. `setSettings('link', 1)` on a Heading); keep `tm`'s per-call `ms` short.
- The driver's `page` is just the first tab. Use `designer()`, `canvas()`, `bridge_frame()` and `live()`, which resolve the current site's tabs.
- More than one live page, more than one width, or anything that shouldn't hold the driver: `live.py`, not `live()`.

## Relay

Sends Designer API calls straight to an open Designer, with Playwright out of the path. They keep working while the driver is busy, and a hung call fails at its own timeout and blocks nothing else.

```
wb.py --HTTP--> relay.py <--WebSocket-- Designer page (relay.js) --postMessage--> MCP Bridge App frame
```

```powershell
python wb.py --status
python wb.py --site my-site --timeout 30 script.js    # or the script on stdin; add --arg '<json>'
```

The script is an async function body: `a` is the arg, `webflow`, `byId` and `tm` are available, and `return` sends the result back. Exit codes: 0 ok, 1 script error, 3 relay down or that Designer not connected, 4 timeout.

- **Setup:** none. `open_site()` starts `relay.py` if needed and connects the Designer. Each site needs one `open_site()`, which grants that Designer's origin local network access.
- **Stays connected:** tested through a killed and restarted relay (about 1 s, 3 s at most), a Designer reload with nothing re-attaching it, and a killed driver: `wb.py` calls and page switches kept working with no driver running, and needed no relaunch afterwards. `js()` still needs `relaunch_bridge()` after a driver restart. `relay.js` is an init script on the tab and goes away with the driver, so a Designer reload or a Chrome restart stays disconnected (exit 3) until the next `open_site()`, which reconnects it.
- **Why the page holds the socket:** Chrome blocks local network access from the cross-origin extension frame even with the permission granted, because the Designer doesn't delegate it. The Designer page holds the socket and relays to the frame with `postMessage`.
- **Security:** `relay.py` binds to `127.0.0.1`. `.relay-token-<port>` (git-ignored, survives restarts) has a `control` token that runs code, which stays in that file, and a `client` token that only lets a page connect, which goes into the page script. The socket also requires a Designer `Origin`, and HTTP requests with an `Origin` header or a non-localhost `Host` are refused.
- **Tested:** `python tests/test_relay.py` runs without Chrome against a fake Designer, on its own ports: refused callers, message sizes up to 3 MB, concurrency, timeouts, two sites, disconnects and restarts. Rerun it after changing `relay.py` or `wb.py`. Against a real Designer: 95 pages, two sites at once, 4 MB results, and a write confirmed with Data API `query_elements`. Loading a LAN dev server after the grant hasn't been tested.

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
- **One client only:** a second Playwright driver on the same Chrome sees the Designer's cross-origin frames (canvas, bridge) as blank, and Chrome stops finishing the attach of new tabs. `live.py` is not a second client: it has its own Chromes. `tests/stress_guardrails.py` starts test drivers on `:9222`, so run it with the production driver stopped.

## Second desktop (`desk.py`)

With `"desktop": 2` in `machine.json`, every automation Chrome window (the driver's, the Designer job windows, the `live()` window, every `live.py` window) lives on the second virtual desktop. Nothing opens, flashes or takes the focus on the desktop the user works on; Ctrl+Win+Right shows the windows working.

- **How:** Windows lets a process move only its own windows between virtual desktops, and an owned window always lives on its owner's desktop. `desk.py` makes one small window of its own ("aite automation windows"), moves it to the target desktop with the documented `IVirtualDesktopManager`, and makes it the owner of every top-level window of the automation Chromes (found the same way the watchdog finds them). It polls about 8 times a second.
- **Focus:** Windows switches desktops to follow the focused window, so a window must never be focused when it moves. `desk.py` hides a new window, moves it, and shows it again without activating it. The tools open windows without activating them (`STARTUPINFO` `SW_SHOWNOACTIVATE` for `live.py`'s Chromes, `Target.createTarget` with `background` for Designer windows) and never call `bring_to_front`, maximize or `activateTarget`. Measured while opening 3 live Chromes and 2 Designer windows: the current desktop and the focused window didn't change.
- **Rendering:** the windows keep painting there (61 frames a second, captures in 0.2-1.4 s), because Chrome runs with occlusion tracking and background throttling off.
- **Lifetime:** the driver and the `live.py` keeper start it when it isn't running. `python desk.py --stop` hands the windows back to the current desktop. Owned windows close with their owner, so if `desk.py` is killed hard, the Chrome windows it held close too; the tools open new ones.
- Without `"desktop"`, or when that desktop doesn't exist, windows open on the current desktop.

## Playwright pitfalls

- **GPU:** a headed window renders on the 4090 by default, even with `--disable-gpu`. Pin it with `--use-angle=d3d11 --use-adapter-luid=0,166d5`. `--disable-accelerated-video-decode` keeps stray video off the GPU decoders, and `driver.py` blocks video URLs in Webflow Designer tabs (`*.design.webflow.com`, whose canvas plays every video on the page) through CDP `Network.setBlockedURLs`. Other tabs, e.g. a staging page, play video normally. Open the Designer through `open_site()` / `designer()` so the tab is blocked before it navigates.
- **Local network prompt:** pages that load a LAN dev server (a Vite origin like `https://192.168.x.x:5173`) stop on Chrome's "Access other devices on your local network" prompt. In the driver, `open_site()`, `relay_attach()` and `live()` call `grant_local_network(url)`, which uses CDP `Browser.setPermission` for that origin; `live.py --dev` does the same in its own windows. Without an origin the call reports success but has no effect on Chrome 156, and `local-network-access`, `local-network` and `loopback-network` are granted separately. Cross-origin iframes stay blocked unless the parent delegates the permission.
- **Stack down:** when `STOP` exists or Chrome is gone, the driver refuses snippets and `x.py` exits 3 with the reason. After Chrome restarts and `STOP` is deleted, the next call reconnects.
- **Viewport:** in the driver's Chrome, `page.set_viewport_size` can silently stop applying (after `bring_to_front`, `viewport_size` reports 390 while the page lays out at 1440). Check layouts at a width with `live.py` (`--widths`, or an item `width`): it verifies `innerWidth` after every load and raises on a mismatch.
- **Screenshots:** Playwright page and element screenshots can hang 30-100 s on Webflow pages and on the Designer. Use CDP `Page.captureScreenshot`: `p.snap()` in `live.py`, `snap(tab, path, clip={...})` in a snippet. On the Designer, clip to the panel you need.
  - **Why a capture stalls:** Chrome paints only the front tab of a window. A tab behind another one gets no frames, and `Page.captureScreenshot` waits for a frame that never arrives (41-167 s measured, and it can't be interrupted). `visibilityState` and `requestAnimationFrame` still report "visible" and 61 fps there, so they can't detect it.
  - **Rule:** capture only in a tab that is alone in its window: every `live.py` window, and the `live()` tab. There, captures took 0.1-0.7 s.
- **Touch input:** use raw `Input.dispatchTouchEvent` (touchStart, touchMoves, touchEnd) with `Emulation.setTouchEmulationEnabled`. It scrolled 520–550 px in 4 of 4 runs; `Input.synthesizeScrollGesture` with `gestureSourceType: "touch"` scrolled 0 px in 4 of 4, on a plain page and on a live site.
- **Headless doesn't work for the Designer** (Webflow rejects the `HeadlessChrome` UA). Never spoof the UA; that is bot evasion.
- **No `context.route()` with the sync driver:** routed requests are only answered while a snippet runs, so an idle driver stalls every tab. Block URLs through CDP.
- **A driver only sees bridge frames created after it attached.** After a driver restart (or from a second client), the bridge iframe is invisible until it is relaunched: run `relaunch_bridge()` with the site set (reloads the Designer, launches the app, minimizes it; about 17 s).
- **One snippet at a time on the main thread.** `x.py --stop` stops it at its next `check()`. If it never gets there, ask the user before killing `driver.py`.
- **Never `await` canvas switches** (`webflow.openCanvas`, `switchPage`). Fire the call, then poll.
- **No `networkidle`** on Webflow. Use `domcontentloaded`, then wait for a specific element.
- **Encoding:** run `x.py` with `PYTHONIOENCODING=utf-8`, or `\u200d` / `–` in results crash it. `live.py` sets UTF-8 output itself.
- **Text input:** paste, never type. For rich text, write `text/html` with `ClipboardItem` after `context.grant_permissions(["clipboard-read","clipboard-write"], origin=...)`. Lone or trailing spaces get trimmed: paste `"day to"`, not `" "`.
- **Never complete bot checks or CAPTCHAs.** Hand them to the user.

## Webflow Designer pitfalls

- **Bridge app:** the Designer API lives in the `webflow-ext.com` iframe (Webflow MCP Bridge App). It disappears after reloads, canvas switches or closing it with X; minimize it instead. If a call fails twice, run `relaunch_bridge()` (it reloads the Designer and relaunches the app); after 2 failed relaunches, stop and tell the user.
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
- **Embeds with bindings:** an HtmlEmbed whose code contains bindings returns `{sourceType: "unsupported", reason: "containsBindings"}` for `code`, from `el.searchSettings()` and from the Data API `get_settings` (where the resolved value is `null`). Static embeds return their code from both. It isn't a component prop, so `getProps()` doesn't list it. Read or edit it in the Designer: binding pill > Edit settings > Edit code, then read `.cm-content`'s `cmView.view.state.doc`.
- `el.getProps()` has no prop names; match `propId` to the `id` in `component.getProps()`, which has `name`, `group`, `type` and `defaultValue`. Link values are `{mode, to}` with mode `page`, `url`, `pageSection`, `email`, `phone` or `collectionPage`.
- `el.searchPropAvailableFields(propId)` lists filter fields and operators (`{filters:[{fieldSlug,operator,value}], filterMatch}`); `el.searchBindableSources()` lists CMS fields and reference paths.
- `getAllElements()` includes elements placed in an instance's slot on the page: a page-wrapper slot's Nav-1 was returned, with the same results on 74 pages as a manual slot walk. Inherited slot defaults aren't page elements (see the next line). Don't walk the tree by hand to find components on a page.
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

- **Time one item, then batch.** Per-page cost varies widely: a full bridge page walk is about 5 s at the median (2.7 s of it the switch), but heavy pages take 70-200 s. `run_items` batches work on one Designer page or component list inside a snippet; many Designer pages are a job, many live pages are `live.py`.
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

- **Why windows, not tabs:** Webflow allows several Designer tabs of one site, each with its own current page and its own bridge app. Chrome throttles a tab that isn't in front of its window, so only separate windows help: on the same 12 pages, one window took 64.6 s, two tabs in one window 81.7 s, two windows 33.6 s and three windows 26.9 s. Per-page time rises with each window, so three is the default.
- **Each getter is a round trip** (about 20 ms): listing 55 pages with their parents took 2.8 s. Fetch once and reuse the handles.
- **Throttled Designer:** a Designer tab that isn't in front of its window is throttled: every call takes about 1 s instead of 15–30 ms, through both `js()` and `wb.py`, and pages took 14 s each against 5.4 s in front. `switch_page` by name then times out, because it reads each page's name in turn. Fix: `designer().bring_to_front()`, which took calls from about 980 ms to 11–19 ms at once. It starts after a while behind another tab: a staging tab in front for 70 s didn't trigger it, and neither did a minimized window. Restarting the driver doesn't help.
  - **Chrome flags:** Chrome now starts with `--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion` ("Start"). After 15 s behind another tab, a background tab ran 324 timers of 10 ms in 5 s, against 323 in the front tab. Designer call times behind another tab haven't been re-measured with the flags, so keep each Designer in front of its window.
- **`switch_page` lookup:** an id matches without reading any names (about 0.06 s). A name or slug reads every page's name and slug in parallel (about 0.25 s among 95 pages, more when throttled). Names and slugs can repeat (two pages with the same name and slug): a repeated one raises `AMBIGUOUS` with the matching ids. Pass ids in bulk work.
- **Time ops inside the frame** with a small wrapper that records per-call ms and catches per call, so one failure doesn't abort the batch or lose earlier timings.
- **Agent time dominates:** in one benchmark 72% of the wall clock was the agent writing snippets between calls, not the tools. Reuse a helper file (frame lookup, `js()` wrapper, poll-until) instead of rewriting it per snippet.
- **Switching pages:** fire `webflow.switchPage(page)`, poll `getCurrentPage().id`, then wait about 1.5 s before `getAllElements()`, or you read the previous page.
- **Component canvas:** fire `webflow.openCanvas({componentId})`, poll `getCurrentComponent().id`, then wait about 5 s.
  - Images are lazy: set `img.loading = 'eager'`, scroll each into view, wait, then read `currentSrc` (otherwise SVG placeholders).
  - Read every non-extension frame and keep the one with real image sources.
  - Slot wrappers are `.slot[data-w-id="<slot id>"]`, matching `instance.getSlots()` IDs. A page inherits a slot default only when that slot has 0 children on the page.
  - Links render resolved (e.g. `/contact-form`), so `#` in the canvas is a real `#`.
- **Canvas checks:** the canvas is the rendered DOM of the current Designer state, so `evaluate_t(canvas(), js)` answers layout and paint questions about unpublished work (on 4 pages it matched the published page). It doesn't run the site's custom code (a script-built form existed only on the live page), shows every FAQ answer open, and adds `data-wf-id` grid rules with `minmax(75px, auto)` rows, which can change heights. Check behaviour (hovers, IX, scripts) on a live page after a publish.
- **Live site checks:** `live.py` ("Live pages"). Scroll with `p.load_all()` to load lazy images, and judge visibility from computed styles. About 6 s per page per window, three windows at once.
  - A static HTML crawl misjudges CSS-hidden elements and misses JS-built links.
  - A live page shows the last publish, so it can't confirm the Designer's current state, drafts, or templates without a public page.
- **Keep big reads out of the conversation.** Data API `get_all_elements` returns 50–100 KB per page; prefer `query_elements` with a filter, or a script that writes to a file and returns counts.

## Files

| File | What it is |
|---|---|
| `wf.py` | The command line agents use. |
| `store.py` | The site store and the offline queries and plan resolution on it. No browser. |
| `ops.py` | The Designer operations (`sync`, `set`, `qa`, `read`): one visit per page, several windows at once. Loaded by the driver. |
| `bridge.py` | Designer helpers for snippets, and the kept Designer windows. Loaded by the driver. |
| `driver.py`, `x.py` | The process that owns the logged-in Chrome's Playwright connection and runs snippets, and its low-level client. |
| `live.py` | Published pages in parallel, with the keeper process that owns their Chromes. |
| `desk.py` | Holds every automation window on another virtual desktop. |
| `watchdog.py` | Kills the automation Chromes if they load the RTX 4090; writes `STOP`. |
| `relay.py`, `relay.js`, `wb.py` | One Designer API call without Playwright (REFERENCE.md "Relay"). |
| `machine.json`, `sites/`, `data/`, `runs/`, `activity.log` | This machine's settings, per-site settings, the store, live-run state, and the log of every window opened, reused or closed. Git-ignored. |

## Tests

- `python tests/test_store.py`: the store's queries and plan resolution, no browser, seconds.
- `python tests/test_live.py`: `live.py` against a local server (about 3 min).
- `python tests/stress_guardrails.py`: the driver's guardrails, with the production driver stopped (about 2.5 min).
- `python tests/test_relay.py`: the relay, without Chrome.

## Why the bridge and not the Webflow MCP for instance props

Its data tools need no browser, but a call is bound to one page, takes 4-5 s plus 0.3-0.5 s per action, and an instance's props come back as 12 KB with no way to filter (a whole page's instances: 200 KB). Seven prop edits on seven pages are 7 calls and about 85 KB of output; here they are one command and a 10-line table. The MCP stays the tool for publishing and for what the Designer API can't do (moving elements, assets, variables).

## A live script

```python
# check.py
ITEMS = ["/", "/pricing", {"path": "/about", "width": 390}]     # paths (staging URL + path) or full URLs
OUT = r"C:\tmp\results.json"       # one file per task; also WINDOWS, WIDTH, HEIGHT, BUDGET, PER_PAGE, DEV, WAIT

def each(p, item):                 # p: the loaded page, alone in its own window; runs in every window at once
    r = p.evaluate("() => ({h1: document.querySelector('h1')?.innerText})")
    p.snap(rf"C:\tmp\{r['h1']}.jpg", full=True, quality=75)
    return r                       # saved in OUT as {"<item key>": {"v": r, "status": 200, "secs": 1.2}}

def done(results, summary):        # optional, once at the end
    ...
```
