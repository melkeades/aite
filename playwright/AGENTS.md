# Agent rules: Playwright + Webflow

Mandatory. README.md has setup and details. Keep both files site-agnostic; site-specific notes go in that project's AGENTS.md or its `sites/<key>.json` (`notes`).

## Stack
- Never load the RTX 4090 (nvidia-smi index 1). Start Chrome only with the README command and keep `watchdog.py 50` running.
- Reuse the running Chrome (`:9222`, `chrome-profile/`) and driver (`:8765`); check with `x.py --status` first. Ask before killing any process. After a driver restart, run `relaunch_bridge()` before any bridge call.
- Name the site on every Webflow call: `x.py --site <key|Designer URL>` (or `open_site()` / `use()` in the snippet). A new project needs no setup: its URL creates `sites/<short>.json`. Never hardcode a site into the helpers, and never work around the SEVERAL DESIGNERS OPEN error by picking a tab.
- Headed only. Never spoof the user agent, never use `context.route()` (block URLs through CDP), never complete CAPTCHAs.
- Never `await` canvas switches (`openCanvas`, `switchPage`). Never wait for `networkidle`.
- Run `x.py` with `PYTHONIOENCODING=utf-8`.
- After changing `driver.py` or `x.py`, run `tests/stress_guardrails.py`. It must show 0 FP and 0 FN.
- Never print, log or commit `.driver-token-*` or anything from `chrome-profile/`, and never weaken the driver's token, Origin or Host checks. Talk to the driver only through `x.py`.

## Time and visibility
- Every `x.py` call has a `--budget` (default 90 s, max 300). Never `run_in_background` it, and never put a whole site into one call.
- Time one item first, then batch through `run_items` and `evaluate_t`, calling again until `remaining` is 0. Between batches, tell the user the progress and time per item.
- Exit 3, 4 or 5: stop and tell the user at once, with the printed reason and `x.py --status` (plus the `gpu.log` tail for 3). Never work around it silently.
- Report `slow` and `errors` items with their times. Don't silently retry or wait on them.
- Never end a turn idle without saying what is running, how long it should take and what comes next.
- When the user asks what you are doing, answer that first, in plain words, before any tool call.

## Editing in Webflow
- Data API for one-offs, the bridge for bulk work (about 100× faster), the Designer UI last.
- Use the `bridge.py` helpers (`js`, `tm`, `byId`, `until`, `switch_page`, `open_canvas`, `select`, `paste_at`) instead of rewriting them. Call `el.searchSettings()` before guessing a setting key or value shape.
- Paste, never type. Rich text: **Edit Rich Text**, Ctrl+A, paste the full HTML once, then remove the trailing empty paragraph.
- Check every instance before an edit. Edit the component only when the change fits all of them, otherwise the instances; say which and why.
- Edit slot defaults in the component canvas, never on a page.
- Internal links are type `page`, never `url`.
- Responsive checks: `emulate(tab, width)` (never trust `set_viewport_size`), then reload. Screenshots: `snap()` (CDP), never full-window Playwright screenshots of the Designer.
- Screenshot before clicking by coordinate. Check `getSelection()` before deleting or pasting; never delete through a script-made selection.
- If the bridge fails twice in a row, reload the Designer tab. Minimize the bridge app, never close it.
- Read every change back (API or `getProps`) before calling it done. Report partial work as partial.
- Never publish unless the user asks.

## Working style
- When the next step is clear and already approved, keep going.
- Answer questions plainly and directly.
