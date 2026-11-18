# Issues: what was wrong, how it was solved, what is still open

Status, November 2026: **works on one site, with one agent at a time. Needs more testing and more optimization before it is trusted.** The sections "Not tested" and "Outstanding" below are the to-do list.

Everything here comes from one long session on one site (ca-nefi). The task used to measure progress: given a list of 25 places where text is cut by a line clamp, turn on the "Disable line clamp" prop on the component instances behind them, and report the ones that have no such prop.

## Measurements

A fresh agent (no prior context) ran that task six times while the tools were being rebuilt.

| Run | Tools | Steps (tool calls) | Time | Agent tokens |
|---|---|---|---|---|
| 1 | read lanes removed, Designer jobs, `live.py` | 35 | 416 s | 209 K |
| 2 | same, plus kept windows | 37 | 479 s | 222 K |
| 3 | `wf.py`, store, one visit per page | 23 | 356 s | 123 K |
| 4 | plus batch lookup, one-stage cold start | 19 | 258 s | 109 K |
| 5 | plus prop answer in the lookup | 16 | 261 s | 95 K |
| 6 | plus the lookup writing the plan | 15 | 267 s | 96 K |

How far to trust these numbers:
- One run per row. There are no repeats, so there is no spread; runs 4-6 are within noise of each other.
- They are the agent's own numbers. They leave out the orchestration around the runs and the resets between them.
- "Agent tokens" is the figure the harness reported for the subagent. What exactly it counts was not checked; read it as a trend.
- Runs 1-2 also read the values back through the Webflow MCP and looked at the canvas; later runs use `wf.py qa` for that. The verification differs, not only the speed.
- Run 6 reported reusing three windows, so its start was probably not cold.
- One agent at a time, on a machine that was also in normal use.

Other measurements used for the design (single measurements each):

| What | Result |
|---|---|
| Switch the Designer to a page | 2.5-6 s |
| Read every instance, prop and text of a page (bridge) | 0.3 s |
| One prop write | 0.08 s |
| Open three Designer windows and launch their bridge apps | about 22 s |
| Full read of 11 pages in 1 / 2 / 3 windows | 104 s / 72 s / 48 s |
| Reload three Designer windows for QA | about 21 s |
| Webflow MCP data call | 4-5 s plus 0.3-0.5 s per action, one page per call, 12 KB per instance |

Where the time of a run goes now: about 135-150 s is tool time (`sync` 55-67 s, `set` 30 s, `qa` 50 s); the rest is the agent's own turns (reading the docs, building the report, writing the answer).

## Solved

### Concurrency and windows
| Problem | Cause | Fix |
|---|---|---|
| Several windows open, only one working | Read lanes handed the agent windows and left the loop to it; a one-window loop was the easiest thing to write | `live.py`: the agent writes what happens on one page; the tool owns the loop and always runs N windows at once. No API for a serial loop. |
| Blank spare windows sitting idle | Lanes shared the Designer's Chrome and could only use windows the driver's main thread had created, so the driver kept a pool | Lanes and the pool removed. Live pages run in Chromes of their own. |
| Stopping a command didn't stop the work | The lane kept running inside the driver | A live run is a process: kill it and the work stops. The keeper frees its windows. `x.py --stop` stops a driver snippet at its next `check()`. |
| Windows opened and closed for every command | Every command was a self-contained batch | Windows are a session: Designer windows (`bridge.py` `_pool`) and live windows (the keeper in `live.py`) stay open 5 minutes and are reused. |
| A cold start opened one window, then two more | The site read and the page visits each started their own session | `op_sync` opens all its windows once. |
| A restarted driver left its old Designer windows open, so six rendered at once | The window list lived only in memory | `.session-windows.json`; the next session closes what an earlier driver left. |
| Windows flashed on the user's desktop | They opened wherever the user was | `desk.py` holds every automation window on another virtual desktop (owner-window trick, documented APIs only). |
| The user's screen jumped to that desktop | A new window took the focus, and Windows follows the focused window when it moves | Windows open without activation (`SW_SHOWNOACTIVATE`, `background: true`); `desk.py` hides a window, moves it, shows it without activating. No `bring_to_front`, no maximize. |

### Steps and data reuse
| Problem | Cause | Fix |
|---|---|---|
| Five open-read-close cycles for one edit | One narrow question per run; nothing was kept | The store (`store.py`, `data/<site>/`): `sync` reads a page completely once, `find` answers offline. |
| Results silently cut at 20 KB | The job worker truncated what a page returned | Jobs removed; the store keeps whole pages. |
| 37 steps for a 2-step job | Raw JSON out, hand-written JavaScript in; a separate dry run, write, read-back and check | `set` takes a plan and does check-old, write, read-back and render check in one visit per page; `qa` is one command. |
| A page read took 4 s | Nesting was computed with `getSlots`/`getChildren` on every element | Nesting is read from the canvas DOM (`data-w-id` ancestors): 0.3 s. |
| CMS-bound text could not be tied to a component | It is in no prop | The store keeps what the canvas renders, with the instance that renders each text node (`data-wf-id` path). |
| Collection-list content missing from a read | The canvas was read before it finished rendering | The canvas read waits until the node count stops changing. |
| Cards inside a collection list had no component | Their ids carry `_instance-<n>` | The suffix is stripped when matching; a render check runs on every copy. |
| A collection template could not be found by path | The Designer reports the path of whichever item the canvas shows | The template's base path comes from its slug (`detail_<collection>`). |
| Ten small scripts to place 25 findings | No batch lookup | `find --from FILE --prop P [--plan]`: the component behind each finding, whether it or its top component has the prop, and the plan. |
| A selector key was silently ignored (7 writes became 14) | Unknown keys were dropped | Unknown keys are an error. `where.element` exists. |
| Three MCP calls to decide whether publishing needs permission | The domains were only known to the MCP | `sync` reads the site info and prints the publish rule. |
| 78 KB of required reading | Every past failure had become a section | AGENTS.md 6.5 KB, README 10.6 KB; internals in REFERENCE.md, not required. |

### Errors that were hidden
| Problem | Cause | Fix |
|---|---|---|
| A page that failed to load counted as loaded | After a reload the lane carried on and bumped a counter | `LoadError` for HTTP 400+, network errors and no load event; the run lists failed items and exits 1. |
| A load timeout took 60 s to report | The error path evaluated in a document that was still loading | That evaluate removed. |
| A hung page could hang a run | No per-page limit | `PER_PAGE`: the window's Chrome is replaced, the item is an error. |
| A live run did nothing and exited 0 | Its output file already held the items, from another task | It prints `NOTHING TO DO` and names the file; `--fresh` ignores it. |
| A live run blocked forever | It read a script from stdin although pages were given | stdin is read only when nothing else says what to do. |
| `/pricing` became `C:/Program Files/Git/pricing` | Git Bash rewrites arguments that start with `/` | Page arguments are restored. |
| A render check failed on hidden placeholder cards, with no detail | The check saw hidden nodes; a falsy result printed nothing | `h.all()` gives visible nodes; `{pass: false, ...}` is printed; `qa --check` replaces a wrong check without writing again. |

## Outstanding

### Not tested
- **Two agents at once.** `wf.py` waits up to 150 s for the driver; that path has never run with a second agent.
- **Text writes** through `set` (`"text": ...`). Only prop writes ran on a real site.
- **`wf.py read` and `wf.py run`** after the rebuild.
- **Large jobs.** The biggest run was 11 pages. `sync --all` (97 pages) will not fit one 280 s budget; the resume path (exit 4, run again) is untested at that size.
- **A second site.** Everything was built and measured on ca-nefi, whose pages are almost entirely component instances.
- **`--dev`** (the local Vite build): the page requested the local files, but Vite was not running, so it was never seen working.
- **`ops.py` has no automated test.** `store.py` has unit tests; the Designer operations were only checked by hand and by the agent runs. The last change (checking every collection-list copy) was verified on two pages and no test suite was rerun after it.
- **Failure paths in the Designer:** a logged-out session, a bridge app that won't launch, a watchdog kill in the middle of `set`, the second virtual desktop missing, `desk.py` killed hard (the Chrome windows it holds close with it).
- **`open_site()` reloading the Designer** when the bridge won't launch: written, never triggered.

### Known weak points
- **"Saved" is inferred.** `qa` reloads the Designer and reads again, on the assumption that a reloaded Designer shows what the server has. It was not compared with a server read after the rebuild.
- **Staleness.** A page counts as fresh for an hour. Hand edits in the Designer inside that hour are not seen; `set` only notices when the prop it writes differs (`WAS STALE`).
- **Heuristics that can be wrong:**
  - the page shell is "the instance that contains 60% of the others";
  - an instance with no node on the canvas (hidden, or not rendered) gets no parents and so looks like a top component;
  - on a collection template, a finding is matched by class when its text is not on the canvas, and takes the only clamped instance with that class.
- **Stats are single runs.** See "Measurements".
- **The driver is one thread.** A `sync`, `set` or `qa` holds it for its whole duration; other agents wait.
- **Startup.** After a reboot nothing is running; the first command fails with exit 3 until Chrome, the watchdog and the driver are started by hand. For a few seconds after the driver starts, calls get HTTP 403.
- **Live windows keep cookies** between runs until they close (a cookie banner accepted once stays accepted).
- **Live Chromes listen on a local debugging port** with no token, like the main one on `:9222`: any local process can drive them.
- **Windows only** (pywin32, `taskkill`, virtual desktops).

### Leftovers to clean up
- `REFERENCE.md` was assembled from the old README by script; bullets that mentioned removed features were dropped by pattern, and nobody has read the result end to end.
- `bridge.py` still calls the kept windows "job" windows (`_job_new_window`, `job_windows_idle`, `MAX_JOB_WINDOWS`).
- `wb.py` and the relay still exist; `wf.py` does not use them.
- A project's own AGENTS.md may still point at `python live.py` and a "Which tool" section that no longer exists (ca-nefi's does).
- The three Webflow skills were patched, not rewritten; their steps still assume the older flow in places.

## Optimization ideas, not done
- **QA without the reload** (21 of its 50 s): verify in a window that did not write the page, if it can be shown that the second window gets the change through the server.
- **Cheaper cold start** (22 s): keep one Designer window open longer than 5 minutes, or make the keep time a setting per task.
- **A real "render finished" signal** in place of polling the canvas node count; one window alone still spends about 9 s a page.
- **`sync --all` looping by itself** across budgets, so a whole site is one command.
- **Fewer agent turns:** a single command for "find, plan, set, qa" when the plan needs no judgment; check templates for common render checks (clamp, visibility) so the agent doesn't write JavaScript.
- **Real parallelism between agents:** a second driver on its own Chrome profile, or operations that release the driver between page visits.
- **Repeat the measurements:** three runs per version at least, a cold and a warm start each, and one run with two agents.
