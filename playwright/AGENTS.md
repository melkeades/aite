# Agent rules: Playwright + Webflow

Mandatory. README.md has setup and details. Keep both files site-agnostic; site-specific notes go in that project's AGENTS.md or its `sites/<key>.json` (`notes`).

## Stack
- Never load the RTX 4090 (nvidia-smi index 1). Start Chrome only with the README command and keep `watchdog.py 50` running.
- Reuse the running Chrome (`:9222`, `chrome-profile/`) and driver (`:8765`); check with `x.py --status` first. Ask before killing any process. After a driver restart, run `relaunch_bridge()` before any `js()` call; `wb.py` doesn't need it.
- Name the site on every Webflow call: `x.py --site <key|Designer URL>` (or `open_site()` / `use()` in the snippet). A new project needs no setup: its URL creates `sites/<short>.json`. Never hardcode a site into the helpers, and never work around the SEVERAL DESIGNERS OPEN error by picking a tab.
- Headed only. Never spoof the user agent, never use `context.route()` (block URLs through CDP), never complete CAPTCHAs.
- Never `await` canvas switches (`openCanvas`, `switchPage`). Never wait for `networkidle`.
- Run `x.py` with `PYTHONIOENCODING=utf-8`.
- After changing `driver.py` or `x.py`, run `tests/stress_guardrails.py`. It must show 0 FP and 0 FN. After changing `relay.py` or `wb.py`, run `tests/test_relay.py`. It must show 0 failures.
- Never print, log or commit `.driver-token-*` or anything from `chrome-profile/`, and never weaken the driver's token, Origin or Host checks. Talk to the driver only through `x.py`.

## Time and visibility
- Every `x.py` call has a `--budget` (default 90 s, max 300). Never `run_in_background` it, and never put a whole site into one call.
- Before the first bulk run, read README.md "Speed and reliability" and "Data API and bridge notes" in full.
- Before a full run, sample the cases that can break: the page the user named or showed, one expected to pass, one expected to fail, a template, and one of each page structure. Print each result and compare it with what's known. Key results by page id (templates share paths with pages), and select pages by id: names and slugs can repeat, and `switch_page` raises `AMBIGUOUS` when they do. After the full run, treat everything flagged or nothing flagged as a bug, not a finding.
- Time one item first and read its output before scaling up, then batch through `run_items` and `evaluate_t`, calling again until `remaining` is 0. Between batches, tell the user the progress and time per item.
- Exit 3 with `WATCHDOG STOPPED CHROME`: stop and tell the user at once, with the `gpu.log` tail. Never delete `STOP` yourself.
- Exit 3 for any other reason (Chrome or the driver isn't running): say why in one line and restart with the README commands, without asking.
- Exit 4 or 5: stop and tell the user at once, with the printed reason and `x.py --status`. Never work around it silently.
- Don't pipe `x.py`: in `x.py ... | head; echo $?` the status is the pipe's, so an over-budget run reads as 0.
- Report `slow` and `errors` items with their times. Don't silently retry or wait on them.
- Never end a turn idle without saying what is running, how long it should take and what comes next.
- When the user asks what you are doing, answer that first, in plain words, before any tool call.

## Reading from Webflow
- Designer state (props, settings, drafts, templates without a public page) is read in the Designer, through the bridge.
- The rendered result of the current Designer state (what is painted, computed styles, layout before a publish) is read from the canvas DOM: `evaluate_t(canvas(), js)`. The canvas doesn't run the site's custom code (script-built elements exist only on the live page), shows every FAQ answer open, and adds `data-wf-id` grid rules with `minmax(75px, auto)` rows, which can change heights.
- A live page shows only the last publish. Use it for behaviour (hovers, IX, scroll, scripts) and to check after a publish, never as the source for the Designer's current state.
- Single Designer API calls, and page loops in one window (`wb.each_page`), go through `wb.py` (README.md "Relay"); work across many pages follows "Concurrency" below. `wb.py`: Playwright isn't in that path, so it works while the driver is busy (`x.py` is refused with exit 5, `wb.py` still answers in about 40 ms) and a hung call only costs its own timeout.
- Use `js()` and `run_items` only inside an `x.py` snippet that also drives the Designer UI or a live page. `open_site()` runs through `x.py` and connects the relay.
- Screenshots only when asked, when no value can answer the question, or for a QA side-by-side against a mock. Never in bulk: take each `snap()` in its own `x.py` call, because it can stall for minutes (68–141 s seen; 1–3 s is typical).

## Concurrency
Why:
- A Designer has one current page, and `webflow` calls see only that page. A page switch costs seconds (1.4–9 s measured), so work across pages gets faster only with more Designers, each on its own page. Two loops on one Designer switch each other's page: in a test, 8 pages took 55 s, no faster than one loop, and one switch never landed.
- Chrome throttles a tab that isn't in front of its window to about one Designer call per second (14 s a page instead of 5.4 s). So each extra Designer needs its own window, in front. On 12 pages: one window 64.6 s, two tabs in one window 81.7 s, two windows 33.6 s, three windows 26.9 s. Each added window raises the per-page time, so 3 is the default.
- The driver runs one snippet at a time on one thread and can't be interrupted. A second Playwright client on the same Chrome sees the Designer's frames blank, so never start one.

How:
- On one page: run the calls together with `Promise.all` inside one body (50 writes took 0.46 s, about 10× faster than one by one).
- Across pages: `run_pages(page_ids, BODY, out, arg, n=3)` in an `x.py` snippet (recipe in README.md "Speed and reliability"). `designer_windows(3)` opens or reuses one Designer per window, launches a bridge app in each, places them side by side and brings each to front. Each window then runs its own page loop inside the Designer (switch, wait, read), so the windows work at the same time while the driver only polls each about once a second and saves results by page id. A final sweep collects anything a poll missed. Call again until `remaining` is 0, then `close_designer_windows()`.
- Measured: 36 pages took 120 s over 3 windows against 291 s in one, with identical results. On 12 light pages it was only 1.3× faster, so for a handful of pages one window is enough.
- `wb.py` reaches one Designer per site (the relay keeps one connection per site), so `wb.each_page` is one window. It runs beside the driver, so use it while an `x.py` snippet is busy.
- Keep every Designer tab in front of its window (`bring_to_front()`), and close the extra windows when done.
- Sessions share one driver: a request while another snippet runs gets exit 5 (refused, not queued; handle it as above). Each snippet names its own site, so sessions never inherit each other's site.

## Editing in Webflow
- Data API for one-offs, the bridge for bulk work (about 100× faster), the Designer UI last.
- Use the `bridge.py` helpers (`js`, `tm`, `byId`, `until`, `switch_page`, `open_canvas`, `select`, `paste_at`) instead of rewriting them. Call `el.searchSettings()` before guessing a setting key or value shape.
- Paste, never type. Rich text: **Edit Rich Text**, Ctrl+A, paste the full HTML once, then remove the trailing empty paragraph.
- Check every instance before an edit. Edit the component only when the change fits all of them, otherwise the instances; say which and why.
- Edit slot defaults in the component canvas, never on a page.
- Internal links are type `page`, never `url`.
- Responsive checks: `emulate(tab, width)` (never trust `set_viewport_size`), then reload. Screenshots: `snap()` (CDP), never full-window Playwright screenshots of the Designer.
- Screenshot before clicking by coordinate. Check `getSelection()` before deleting or pasting; never delete through a script-made selection.
- If a bridge call fails twice in a row, run `relaunch_bridge()` (it reloads the Designer and relaunches the app, about 14 s). If two relaunches fail, stop and tell the user. Minimize the bridge app, never close it.
- Read every change back from the server (Data API `query_elements` / `get_settings`, which also return prop names) before calling it done. `getProps` and the canvas show the Designer's copy, which can differ from what was saved. Report partial work as partial.
- Before publishing, check the site's domains with `get_site`. If it has both a production domain and a staging domain, publish to staging without asking when the user asks to build or change something. Otherwise, don't publish without the user's permission. A request only to find or plan a fix publishes nothing.

## Working style
- When the next step is clear and already approved, keep going.
- Answer questions plainly and directly.
