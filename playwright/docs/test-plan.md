# Test plan (sketch): stress and optimize the harness on other projects

High level on purpose. Each line is a task to hand to a fresh agent with the user's kind of wording, on a project that is not ca-nefi. Nothing here is built yet.

Two modes:
- **Sync**: a long, complex task, vaguely worded, with several unspecified sub-goals, where concurrency is needed: many pages across several windows, and possibly several agents working on parts of it at once. Tests whether the concurrency really delivers, data reuse over a long session, agents sharing the driver, the windows and the store, and how many steps the harness forces.
- **Async**: a short, simple, concrete task for a single agent, with no concurrency because nothing in it needs any. Tests the floor cost of a small job, and that the harness doesn't open windows or start parallel work it has no use for.

## What to record for every run
- Steps (tool calls), wall time, tool time against agent time, tokens. For several agents: per agent and in total, and the wall time of the whole task.
- Browser cost from `activity.log`: windows opened and reused, visits per page, repeated reads of the same page, windows that were open but idle.
- Concurrency (sync): how many windows were working at the same moment, time agents spent waiting for the driver, speed-up against the same task done by one agent in one window.
- Correctness against a known answer prepared beforehand; wrong, extra or lost writes count as failures.
- Whether the task ended with QA, and what the agent said it did not check.
- Errors, retries, exit codes, and every question the agent had about the tools or docs.
- Desktop or focus changes on the user's screen (should be none).

Run each case three times, once from cold (nothing open, empty store) and at least once warm.

## Targets to cover
| Target | Examples |
|---|---|
| Webflow, component instances | props, overrides, variants, nested instances, slots |
| Webflow, regular elements | text, links, images and alt text, classes, styles, attributes |
| Webflow, CMS | collection templates, lists inside components, bound text |
| Webflow, structure | add, move, remove, duplicate a section |
| Webflow, published pages | staging and production, several widths, after a publish |
| Not Webflow | any public site, a local dev server, a reference site to compare against |

Sizes: about 10 pages and a whole site (100+) for sync; 1 page, at most a handful, for async.

## Async: short, concrete, one agent, no concurrency
One sentence each, with exact names. This is the floor: the fewest steps and seconds a small job can cost.

| # | Task | What it stresses |
|---|---|---|
| A1 | "Which pages use component X?" | offline answer; cost when the store is empty |
| A2 | "Which component shows the text '...' on /page?" | rendered-text lookup, CMS text |
| A3 | "On /page set prop P of component X to V" | floor cost of one write with QA; one window, not three |
| A4 | "On /page change the heading '...' to '...'" | a text write on a regular element |
| A5 | "On /page remove the override of prop P on X" | the reset path |
| A6 | "Screenshot /page at 390 and 1440" | live window, no Designer |
| A7 | "Does <non-Webflow URL> load, and what is its h1?" | smallest non-Webflow job |
| A8 | "Set the bottom padding of class C to N" | work outside `set` (snippet or MCP), routing choice |
| A9 | "Change field F of CMS item I to V" | should go to the MCP, not the harness |

Pass means: correct, QA present where something was written, no window opened that did no work, and a step count and time that stay flat across the three repeats.

## Sync: long, vague, concurrent
Run each first with one agent using several windows, then split across 2, 3 and 5 agents (by page group, by sub-goal, or one finding and one fixing).

| # | Task wording (to be adapted per project) | What it stresses |
|---|---|---|
| S1 | "Cards look cut off in places, fix what you can and tell me the rest" (whole site) | discovery on live pages in parallel, mapping to components, partial fix plus report |
| S2 | "Make the button wording consistent across the site" | text in props and in regular elements, judgment, many small writes over many pages |
| S3 | "The pricing and service pages look off on mobile, sort it out" | live checks at several widths at once, style edits outside `set`, verify after publish |
| S4 | "Blog cards show too much text on some pages" | CMS templates, collection lists, bound content, template against live items |
| S5 | "Add a section like the one on /about to these ten pages" | structure work, what the harness can't do in one step |
| S6 | "Make our FAQ behave like the one on <other site>" | a non-Webflow reference read beside Webflow edits |
| S7 | "Check <non-Webflow site> for broken things and give me a report" (50+ pages, 3 widths) | the live tool alone at scale, budgets and resume, failed pages |
| S8 | "Read the whole site, then answer these eight questions" (questions split across agents) | `sync --all` at 100+ pages; several agents reading one store |
| S9 | S1 or S2 with a change of mind halfway and a driver restart in the middle | reuse of the store and task record, recovery with several agents running |

Concurrency cases to force inside these:
- Two agents writing on the same page: different instances; the same instance, different props; the same prop, different values. What does the second one see, and is anything lost?
- Two agents asking for the same pages at once: is each page read once or twice?
- More agents than windows: how long do they wait, and does anyone time out?
- Live work and Designer work at the same moment.
- Agents on two different sites at once.

## Mixes
- An async task arriving while a sync task holds the driver and the windows: how long does the small job wait?
- The same async task right after a sync task on the same pages: does it reuse the store and the open windows?

## Failure injection
Add one at a time to any case above: a 404 or never-loading page in the list; the driver down or restarted mid-task; a Designer window closed by hand; a logged-out Designer; the watchdog `STOP` file; a budget too small for the job; a stale store (a hand edit in the Designer between `sync` and `set`); the second virtual desktop missing; one of several agents killed mid-write.

Expected in every case: a clear error or a clean resume, no wrong write, no hang, the right exit code, and the other agents unaffected.

## Order
1. Async, cold and warm: the floor.
2. Sync, one agent with several windows.
3. Sync, several agents.
4. Mixes.
5. Failure injection.
6. Repeat 1-4 on a second and third project, one of them not built from components.

## How to optimize from the results
After each batch: time every run step by step, list the three largest costs, change one thing, rerun the same batch. Keep a table per case of steps, time, browser visits and (for sync) speed-up across versions, with all three repeats.
