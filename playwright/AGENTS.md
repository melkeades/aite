# Agent rules: Webflow through wf.py

Mandatory. Site-agnostic; site notes go in that project's AGENTS.md or `sites/<key>.json`. README.md describes the commands; REFERENCE.md is for Designer UI work and internals, read only the section you need.

## Mode: the user picks it with one word, you never choose
- `wfasync`: one agent, no concurrency. No subagents. One Designer window, one live window.
- `wfsync`: concurrent. Up to 3 Designer windows, live windows up to the machine cap, and you may start subagents for independent parts. Designer commands (`sync`, `set`, `qa`, `read`, `run`) stay with you, because the driver runs one at a time: subagents take live, offline and Webflow MCP parts. Never two agents writing one page.
- Read the word from the user's own message with `python wf.py mode "<their message>"` (add `--previous <mode>` on a follow-up). It ignores pasted text, fenced blocks and quoted lines. No word means `wfasync`. Both words: it says UNDECIDED; ask the user which one.
- Say the mode once at the start ("mode: wfsync"), then pass it on every command that opens windows: `--mode wfsync` or `--mode wfasync`. The mode holds for the task and its follow-ups until a message carries the other word.
- If the task turns out bigger or smaller than the mode suits, finish in the chosen mode and say what the other mode would have changed. Don't switch by yourself.

## The flow: collect once, reuse, write once, check once
Run everything from this folder. A normal task is these four commands, one browser step each at most:

1. `python wf.py sync <site> <page> ...`: read the pages into the store (`data/<site>/`). A page is an id, a publish path (`/pricing`, or any item URL of a collection for its template) or a name. Pages read in the last hour are not read again. `--all` takes every page.
2. `python wf.py find <site> --text T | --prop P | --component C [--page <page>] [--top]`, or `--from findings.json --prop P` for a whole list of findings at once (records with `text` and a page; a report's data file works): it prints the component behind each finding and whether that component, and the top component around it, has such a prop. Add `--plan plan.json [--value V] --task name` and it also writes the plan that sets that prop on exactly the top instances behind the findings, so you don't write the plan or copy ids by hand: answer questions from the store. No browser. It knows every component instance, every prop (id, value, override or default), what sits inside what, and every text the canvas renders with the instance that renders it (CMS-bound text included, with clamp and cut state).
3. `python wf.py set <site> plan.json`: write. One visit per page checks the old value, writes, reads back and runs the render check, in one window (`wfasync`) or up to 3 at once (`wfsync`). `--dry` only resolves the plan against the store.
4. `python wf.py qa <site> --task <name>`: the end-of-task check. It reloads the Designer windows, reads every written value again and repeats the render check. Every task that wrote something ends with it, once.

Published pages (audits, screenshots, hovers, checks after a publish): `python wf.py live --site <site> ...` (README "Live pages"), all pages in one run, in parallel.

Rules that keep it to those steps:
- Look in the store first (`wf.py pages <site>`, `wf.py find`, `wf.py status`), and in what earlier steps saved (reports and their data files, `data/<site>/live/`). Never open a browser for something a file has.
- One `sync` for all the pages of the task, one `set` for all its writes, one `qa`. If you are about to run a second one, say why first.
- Don't write JavaScript or page loops for reading or writing component instances, props or text: the plan covers it. `wf.py read` (a read-only body per page) is for what the store really doesn't hold.
- A plan sets props on the instance (an override). It never touches a component's definition or default. `null` removes an override. Select instances with `where` (component, page, text it renders, `top`) instead of copying ids when you can.
- `top` means the instance sits in no other instance (the page shell that wraps every page doesn't count). An instance placed inside another instance's slot on the page is listed with `inside`; whether it counts as a "subcomponent" for the task is the user's call: leave it, and report it as "the nested <Component> has the prop; say if it should be set".
- `sync` prints the site's publish rule from its domains; no MCP call is needed to decide whether a publish needs the user's go-ahead.
- A render check sees hidden nodes too unless it uses `h.all(el, selector)` (visible matches only). If only the render check fails, the values are written: fix the check and pass it to `qa --check`, don't rerun `set`.
- Report from the commands' output: what was written, what `qa` confirmed, what was not checked (the published site until a publish).

## Other tools
- Designer UI, canvas, styles, structure, anything `set` can't do: `python wf.py run <site> snippet.py` with the helpers in REFERENCE.md ("Webflow helpers"). One snippet at a time; `wf.py` waits for the driver when another command holds it.
- Webflow MCP data tools: publishing, moving and removing elements, assets, embed code, variables. Not for reading or setting instance props: one page per call, 4-5 s and about 12 KB of output per instance.
- `python wf.py status` shows the driver, the kept windows, the live runs and what the store holds. `activity.log` records every open, reuse and close.

## Windows
- Designer and live windows stay open for 5 minutes after a command and the next command reuses them. Never close them between steps (`wf.py close` is for the end, or when the user asks).
- They live on another virtual desktop (`desk.py`). Never activate one: no `bring_to_front()`, no maximizing, no `Target.activateTarget`. Activating a window there switches the user's screen to it.
- Never connect a second Playwright client to the Chrome on `:9222`.

## Errors and limits
- A page that doesn't load is an error, never a pass: `wf.py live` lists it under `FAILED ITEMS` and exits 1. Don't read values from it.
- Exit 1: the rows say what failed; report them. Exit 4: out of time with pages left; run the same command again, it continues. Exit 3: the stack is down; with `WATCHDOG STOPPED CHROME` stop and tell the user (never delete `STOP`), otherwise say why in one line and restart with the README "Start" commands. Exit 5: another agent held the driver for too long; say so.
- `set` reports `WAS STALE` when the store's value differed from the Designer's: the write was still made from the plan; mention it.
- Never `run_in_background` these commands and don't pipe them (the exit code would be the pipe's).
- When the user asks what you are doing, answer that first, in plain words.

## Hard rules
- Never load the RTX 4090. Start Chrome only with the README command and keep `watchdog.py 50` running.
- Headed only. Never spoof the user agent, never complete CAPTCHAs, never use `context.route()` in the driver.
- Never print, log or commit `.driver-token-*` or anything from `chrome-profile/`; never weaken the driver's token, Origin or Host checks.
- Ask before killing any process, except your own live runs (`wf.py live --stop`).
- Before publishing, check the site's domains with the MCP `get_site`. With both a production and a staging domain, publish to staging without asking when the user asked to build or change something. Otherwise don't publish without the user's permission. A request only to find or plan a fix publishes nothing.
- After changing `driver.py` or `x.py`: `tests/stress_guardrails.py` with the production driver stopped (0 FP, 0 FN). After `live.py`: `tests/test_live.py`. After `relay.py` or `wb.py`: `tests/test_relay.py`. After `store.py`: `tests/test_store.py`. After `router.py`: `tests/test_router.py` (every row must pass). After `hand.py`, `desk.py` or `watchdog.py`: `tests/test_hand.py` (every row must pass). Run them once, when the change is finished.
