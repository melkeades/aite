# The router (sketch): choosing async or sync for a task

Not built. This is the method for designing it and for testing it. Modes are as in `test-plan.md`:
- **Async**: short, simple, one agent, no concurrency.
- **Sync**: long or complex, concurrent: several windows, possibly several agents.

## Decisions made
1. **Time above everything else; tokens second.** A plan is better if it finishes sooner. Tokens only decide between plans whose times are about equal.
2. **"Several agents" means subagents started by the main agent**, not separate sessions.
3. **Start manual.** The user picks the mode with one dedicated word per mode in their message. The agent does not choose.
4. **So there are no routing errors to tolerate.** With the word given, the mode is exactly that word's mode, every time. Anything else is a bug.

## Phase 1: manual mode

### The two words
One word per mode, each a single token that does not turn up in ordinary task text. `sync` and `async` themselves are poor choices: "sync" is a `wf.py` command and a common verb. Decided: **`wfsync`** and **`wfasync`**.

### Rules
- The word counts only in the user's own message, as a whole word, in any letter case. Inside pasted text, a file, a tool result or a quoted example it is data, not a choice.
- It applies to the task in that message and to follow-ups on that task, until a message carries the other word.
- Both words in one message: stop and ask which one.
- No word at all: **async** (one agent, no concurrency), and the agent says so in one line. This default is a proposal; the alternative is to stop and ask every time.
- The agent states the mode once at the start ("mode: wfsync"), so a wrong reading is visible before any work.

### What each mode obliges
| | `wfasync` | `wfsync` |
|---|---|---|
| Agents | the main agent only; no subagents | the main agent may start subagents for independent parts |
| Designer windows | one | up to three, all working |
| Live windows | one | up to the machine cap |
| Splitting | none | by page group or by part; never two agents writing one page; Designer commands go through one agent, because the driver runs one at a time, so subagents take live, offline and MCP parts |
| If the task turns out bigger or smaller than the mode suits | finish in the chosen mode and say what the other mode would have changed | same |

### Built (14 November 2026)
- **`router.py`**: `read_mode(message, previous)` reads the word (whole word, any case; pasted content, fenced blocks and quoted lines don't count; both words give no mode; no word gives the earlier mode of the task, else `wfasync`), and `windows_for(mode, asked, kind)` gives the window cap.
- **`python wf.py mode "<message>" [--previous MODE]`**: the same reading from the command line, so the agent doesn't judge it.
- **`--mode wfasync|wfsync`** (or `WF_MODE`) on `wf.py sync`, `set`, `qa`, `read` and `live`. `wfasync` forces one window and ignores a larger `--windows`; `wfsync` allows up to 3 Designer windows and the live cap. Each command prints the mode it runs in and logs it to `activity.log`. With no `--mode` the command runs as `wfasync`.
- **AGENTS.md**: the rule (read the word with `wf.py mode`, say it once, pass it on every command, never switch by yourself, subagents only under `wfsync` and never for Designer commands).
- **`tests/test_router.py`**: 46 rows, all passing: 35 messages (positions, case, punctuation, lookalikes such as "sync the pages" and `wfsync.py`, pasted and fenced text, follow-ups, both words) and 11 window-cap and obligation checks.

Checked on real pages, read-only, one run each:

| Command | Mode | Windows used |
|---|---|---|
| `sync`, 3 pages | `wfasync` | 1 |
| `sync`, 3 pages | `wfsync` | 3 |
| `sync --windows 3`, 2 pages | `wfasync` | 1, and it says the request was ignored |
| `sync`, 2 pages, no `--mode` | default | 1 |
| `live`, 3 pages | `wfasync` | 1 |
| `live`, 3 pages | `wfsync` | 3 |

One thing that run showed, worth keeping for phase 2: on 3 pages from a cold start, three windows were not faster than one (46.9 s against 40.3 s), because the two extra windows had to be opened first. A single run, so not a rule yet.

Not built or not run:
- The "obeying it" tests with real agents (zero subagents under `wfasync`, every window working under `wfsync`, no page written by two agents). Only the harness side was checked.
- Under `wfasync`, windows left open by an earlier `wfsync` command stay open until they time out; the command uses one and opens none.
- The agent side depends on the agent following AGENTS.md: nothing in the harness can stop a main agent from starting subagents.

### Testing phase 1
Two things can go wrong, and both must test at 100%:
1. **Reading the word.** A table of messages and the mode each must produce: the word at the start, in the middle, in capitals; the word only inside pasted text or a file (must not count); both words; no word; a follow-up message without a word after one with; a lookalike ("sync the pages", "async function"). Checked with no browser.
2. **Obeying it.** Run the `test-plan.md` cases under each word and check from the logs, not from the agent's account:
   - `wfasync`: zero subagents started; never more than one Designer window and one live window used, and none opened beyond that.
   - `wfsync`: with two or more pages, every open window did work; subagents only on independent parts; no page written by two agents.

Every run also records time first and tokens second. Running each case under both words is what produces the measured data an automatic router needs later.

## Phase 2: automatic routing (later, from the phase 1 data)
Everything below is the design for when the agent, not the user, picks the mode. The objective is fixed by decision 1: the plan with the lowest wall time wins; among plans within a small time margin of each other, the one with fewer tokens.

## What the router decides
Not only the mode. One decision, written down before work starts:
- mode: async or sync;
- for sync: how many windows (1-3 Designer, up to 6 live) and how many agents, and how the work is split between them;
- which channel each part uses: `wf.py set`, a Designer snippet, the live tool, the Webflow MCP, or nothing but the store;
- when to look again: the point at which the decision is re-checked (see "Two stages").

## Principle: decide by estimated cost, not by how the task sounds
Vague wording does not mean a big job, and a one-line task can touch every page ("set prop P on X everywhere"). So the router compares estimated cost of the candidate plans and picks the cheapest that is allowed. The estimates come from measured numbers kept in one file, so they can be re-measured per machine and site:

| Number (ca-nefi, single measurements) | Value |
|---|---|
| Page visit, one window | about 9 s |
| Effective per page with 2 / 3 windows | about 6.5 s / 4.4 s |
| Opening Designer windows (any count, in parallel) | about 22 s, 0 when warm |
| QA reload | about 21 s |
| Offline query | 0.3 s |
| One agent turn | about 6 s |
| Starting another agent (reading the docs, first status) | about 15-20 s and its own context |

What parallelizes, and what does not (the router must know this):
- **Designer work**: several windows inside one command, yes. Several agents, no: the driver runs one command at a time, so a second agent only waits.
- **Live pages**: yes, each run has its own windows, up to the machine cap.
- **Offline work** (queries on the store, building a report): yes, freely.
- **Webflow MCP**: one page per call and no gain seen from parallel calls.

So "more agents" pays only when the parts are independent and at least one of them is live, offline or MCP work that is bigger than an agent's start-up cost.

## Inputs
From the wording (needs judgment, so an agent extracts these):
- Is the target named exactly (page, component, prop, value) or described ("cards that look cut off")?
- How many separate goals?
- Scope words: one page, listed pages, "across the site".
- Kind of work: find only, find and edit, audit, build.

From the project (cheap and exact, no browser):
- pages in scope, and how many of them are fresh in the store;
- how many instances or elements match;
- what kind of write it is: props or text (`set`), styles or structure (snippet), CMS content (MCP), none;
- whether windows are open, whether the driver is busy.

## Two stages
1. **Before any work**, from the wording and what the store already has: a provisional mode. Unknown scope counts as unknown, not as small.
2. **After the first offline look** (`wf.py pages`, `find`), when the real page and instance counts are known: confirm or switch. A task that turns out to touch 40 pages escalates to sync; one that turns out to be a single instance drops to async.

Later re-checks only when the scope changes (the user adds a goal, discovery finds more).

## Hard rules, above the cost comparison
- One page, one write: async, one window. Never concurrency.
- Never two agents writing the same page.
- Never open windows for a question the store answers.
- Scope unknown and no data yet: do the offline or single read first, then decide; don't start several agents on a guess.

## Shape
Two parts, so each can be tested on its own:
- **Feature extraction** (agent): wording to a small record: `{named_target, goals, scope, kind}`.
- **Decision** (code, proposed as `wf.py route`): that record plus the project facts and the measured numbers, to a plan and an estimate. Deterministic.

The decision is printed in one line and logged, for example: `route: sync, 3 windows, 1 agent, wf set; 11 pages (0 fresh), 7 writes; estimate 150 s`. After the task, the actual time is logged beside it.

## Testing the router
**The reference answer is measured, not judged.** For each test task, run it in every candidate plan (async; sync with 1, 2, 3 windows; sync with 1, 2, 3 agents) and record time, tokens and correctness. The cheapest correct plan is the answer the router should have given.

The task set:
- the cases in `test-plan.md` (A1-A9, S1-S9);
- boundary cases around the switch points: 1, 2, 3, 5, 10, 30, 100 pages; warm and cold; fresh and stale store;
- wording that misleads: vague but tiny ("something is off with the button on /pricing"), concrete but huge ("set P on every Card on every page"), several goals of which only one is big;
- tasks whose scope changes after discovery or after a user message.

What to measure:
- **Regret**: extra seconds against the measured best plan, then extra tokens. This matters more than how often the router is "right": a wrong choice between two near-equal plans costs nothing.
- **Agreement** with the best plan, as a percentage, for the record.
- **Cost of routing itself**: steps and seconds spent deciding. It has to be far below what it saves on a small task.
- **Stability**: the same decision across three repeats, and across five rewordings of the same task.
- **Switching**: does it escalate when the scope grows and drop back when it shrinks, and at the right moment.
- **Rule violations**: any run that breaks a hard rule is a failure regardless of speed.
- **Estimate error**: estimated against actual time, per plan. A model that is consistently off gets its numbers re-measured.

Three layers of tests:
1. **Decision table** (no browser, seconds): feature records in, expected plan out, including every boundary. Runs on every change.
2. **Extraction** (agent only, no browser): the task set and its rewordings in, feature records out, compared with records written by hand.
3. **End to end**: the whole suite with the router, against two fixed policies (always async, always sync). The router should beat both overall and stay within a set margin of the measured best.

## Still open
- What happens with no word: built as "default to `wfasync` and say so". Change it if a message with no word should stop and ask.
- For phase 2 only: how close two plans' times must be before tokens decide, and what regret against the measured best is acceptable once the agent chooses.
