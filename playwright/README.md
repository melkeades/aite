# Webflow automation: wf.py

Tools for working on Webflow sites with the fewest browser steps. Site-agnostic (site notes go in that project's AGENTS.md).

- **The store.** What is read from a site is saved under `data/<site>/` and reused: a page's component instances, props, text and what its canvas renders. Questions are answered from disk.
- **One visit per page.** A Designer page is slow to open and fast to read, so every command opens a page once and does everything there: read, write, read back, check the render.
- **Several windows at once**, kept open between commands, on another virtual desktop.
- **One QA command** at the end of a task.

AGENTS.md has the rules. REFERENCE.md has the Designer UI helpers, the pitfalls and the internals; it is not required reading.

## Start

Check first: `python wf.py status`.

Run from this folder. On a new machine `chrome-profile/` doesn't exist yet: Chrome creates it, then log in to Webflow once in that window. It is git-ignored (cookies, logins, tokens). The adapter LUID is per machine: find yours or drop `--use-angle`/`--use-adapter-luid`.

```powershell
# 1. Chrome (headed, pinned to the 5090; LUID 0x166d5 on this machine; muted; no background throttling)
Start-Process chrome.exe "--user-data-dir=`"$PWD\chrome-profile`" --remote-debugging-port=9222 --no-first-run --window-size=1600,1000 --use-angle=d3d11 --use-adapter-luid=0,166d5 --disable-accelerated-video-decode --autoplay-policy=user-gesture-required --mute-audio --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion about:blank"
# 2. Watchdog, then driver
Start-Process -WindowStyle Hidden python 'watchdog.py 50'
Start-Process -WindowStyle Hidden python driver.py
```

`live.py` starts its own Chromes with the same GPU flags, read from `machine.json`:

```json
{"chrome_args": ["--use-angle=d3d11", "--use-adapter-luid=0,166d5"], "watchdog": true, "desktop": 2}
```

It refuses to start without that file, while `STOP` exists, or (with `"watchdog": true`) when `gpu.log` hasn't been written for 20 s. It needs neither the Chrome above nor the driver.

## wf.py: the four commands

```powershell
python wf.py sync  my-site /pricing /about "Blog - Categories Template"   # read pages into the store (--all, --fresh)
python wf.py pages my-site [text]                                         # the site's pages, and how old their data is
python wf.py find  my-site --prop "line clamp"                            # offline: which instances have such a prop
python wf.py find  my-site --text "Lorem ipsum" --page /pricing           # offline: which instance holds or renders it
python wf.py find  my-site --component Features-6 --top                   # offline: instances of a component
python wf.py find  my-site --from findings.json --prop clamp              # offline: the instance behind each finding of a
                                                                          # list, and whether it or its top component has such a prop
python wf.py find  my-site --from findings.json --prop clamp --plan plan.json --value true --task disable-clamp
                                                                          # ...and write the plan for exactly those instances
python wf.py find  my-site --component Grid --page /blog --props          # offline: an instance's props
python wf.py set   my-site plan.json            # write (--dry: resolve only, no browser)
python wf.py qa    my-site --task disable-clamp # end-of-task check
python wf.py read  my-site body.js /pricing     # a read-only JS body per page (`a` = arg, `els` = the page's elements)
python wf.py run   my-site snippet.py           # a Designer UI snippet (REFERENCE.md "Webflow helpers")
python wf.py status | close
python wf.py mode "wfsync check every service page"   # which mode a message asks for (offline)
```

**Mode.** Every command that opens windows takes `--mode wfasync` (the default: one Designer window, one live window, no concurrency) or `--mode wfsync` (up to 3 Designer windows, live windows up to the machine cap). The user chooses the mode with that word in their message; `python wf.py mode "<message>"` reads it (`router.py`: whole word, any case, not inside pasted text, fenced blocks or quoted lines; no word means `wfasync`; both words means ask). The command prints the mode it runs in and logs it to `activity.log`. `docs/router.md` has the design and the later automatic phase.

**What a page visit is.** Switching the Designer to a page takes 2.5-6 s and rendering it a few seconds more; reading every instance, prop and text of the page then takes 0.3 s and a write 0.08 s. So every command visits a page once and does all of its work in that visit, and what the visit read is saved.

**The store** (`data/<site>/`, git-ignored, UTF-8: open its files with `encoding="utf-8"`; in a page file `component` is the component's name and `component_id` its id):
- `site.json`: every page (id, name, slug, publish path, static or CMS template, draft), every component with its prop definitions, and the site's domains (`sync` prints the publish rule from them). Read without switching pages, in about 2 s.
- `find --from FILE.json [--prop P]` takes a list of findings (records with `text` and `page`, `path`, `url` or `designer_page_id`, optionally `classes`; a report's data file with one list in it works) and prints the instance behind each, grouped by component; with `--prop` also whether that instance, and the top component around it, has a prop whose name contains P, with its value. `--plan FILE` then writes a plan that sets that prop (`--value`, JSON, default `true`) on exactly the top instances behind the findings; findings whose top component has no such prop are listed as not covered. `--json` prints one record per finding: `i`, `text`, `page`, `element`, `component`, `inside` (component names), `top`, `by`, `prop` (`[{propId, name, value, hasOverride}]`), `top_component`, `in_plan`. It matches the text the canvas renders, then prop values, then, for a CMS template whose canvas shows other items than the live page, the clamped node with the same class.
- `pages/<page id>.json`: `instances` (element id, component, every prop with `propId`, `name`, `type`, `value`, `hasOverride`, `defaultValue`, and `inside`: the instances it sits in), `strings` (text elements), and `rendered`: every text node on the canvas with the instance that renders it, its classes, its line clamp and whether it is cut. `rendered` is what places CMS-bound text, which is in no prop.
- `tasks/<task>.json`: what a task wrote and what its QA found.
- A page counts as fresh for an hour. `set` saves the page again after writing, so the store stays exact through your own edits; edits made by hand in the Designer are picked up by `sync --fresh` or after the hour.

**A plan** (`set`):

```json
{"task": "disable-clamp",
 "set": [{"where": {"component": "Features-6", "pages": ["/pricing", "/about"], "top": true, "text": "Related services"},
          "props": {"Disable line clamp": true}},
         {"page": "/about", "element": "<element id>", "props": {"<prop name or id>": null}},
         {"page": "/about", "element": "<text element id>", "text": "New text"}],
 "check": "(el, h) => h.all(el, '.card__p').every(n => h.css(n, '-webkit-line-clamp') === 'none')"}
```

- `where` selects instances from the store: `component` (exact name wins over a substring), `pages`, `top` (not inside another instance), `text` (in a prop value or rendered by it), `element`. An explicit `page` + `element` works too. An unknown key is an error, never ignored.
- A prop is named by its name or its id; a name that matches several props of the instance is an error that lists them. Values are type-checked against the prop definition. `null` removes the override.
- The plan is resolved against the store before any browser step; an error there writes nothing.
- `check` is optional: a function `(el, h)` that gets the instance's root node on the canvas and returns something truthy when it renders as intended. `h.all(el, selector)` gives the visible matches only (components often keep hidden placeholder copies that never change), `h.css(node, prop)` a computed value, `h.visible(node)` a test. Return `{pass: false, ...}` to have what the check saw printed. It runs in the same visit, after the write, and again in `qa`; `qa --check "<js>"` replaces a check that turned out wrong without writing again.
- Props are set on the instance. A component's definition and default are never touched.

**What `set` does per page:** compares the Designer's current value with the store's (`WAS STALE` if they differ), writes, reads the value and override flag back, runs `check` on the canvas, saves the page to the store. **What `qa` does:** reloads the session's Designer windows, so what they show was loaded from Webflow after the write, then reads every written value again and repeats `check`. It doesn't look at the published site.

**Windows.** Operations run in up to 3 Designer windows of their own (one page per window at a time; your Designer tab keeps its page). Measured on 11 pages: 104 s in one window, 72 s in two, 48 s in three. The windows stay open for 5 minutes and the next command reuses them (`opened 0, reused 3`); a restarted driver closes the ones the old driver left. They sit on another virtual desktop (REFERENCE.md "Second desktop").

**Several agents.** The driver runs one command at a time. `wf.py` waits up to 150 s for it instead of failing, and the store is written atomically per page, so two agents can read and write different pages of a site one command after the other.

## Live pages (`wf.py live`, `live.py`)

Published pages, always in parallel: a run takes N Chrome windows (default 3), gives the next item to whichever is free and saves each result as it arrives. The Chromes are its own (not the driver's), stay open 5 minutes between runs and are reused.

```powershell
python wf.py live --site my-site check.py                          # a script: each(p, item) and ITEMS
python wf.py live --site my-site --js scan.js /a /b /c             # one JS file on every page; saves what it returns
python wf.py live --site my-site --shots C:\tmp\shots --widths 1440,390 /a /b   # full-page captures per width
python wf.py live --status | --stop [PID] | --close
```

A script defines `ITEMS` (paths or URLs, or `{"path", "width"}`), `each(p, item)` (what to do on one loaded page; its return value is saved under the item in `OUT`) and optionally `done(results, summary)`; `OUT`, `WINDOWS`, `WIDTH`, `BUDGET`, `PER_PAGE`, `DEV` are optional settings. A full example is in REFERENCE.md ("A live script").


- `p` is a Playwright page plus: `p.goto(target)` (raises `LoadError`), `p.evaluate(js, arg, timeout=30)`, `p.snap(path, full, clip, quality)` (clip in document CSS px), `p.emulate(width)`, `p.load_all()` (scrolls so lazy content loads), `p.wait(ms)`, `p.note(msg)`, `p.status`, `p.item`, `p.width`.
- A load that answers 400 or above, fails on the network, or fires no `load` event in 30 s is a `LoadError`: the item is recorded as an error, the others go on, and the run ends with `FAILED ITEMS` and exit 1. A page over `PER_PAGE` (60 s) has its window replaced. Nothing a page does can hang a run.
- `--budget` (240 s) stops handing out items; exit 4, run the same command again. Items already saved in `OUT` are skipped (`NOTHING TO DO` names the file; `--fresh` ignores it).
- `--widths` runs every item at each width side by side. `--dev` loads the local Vite build (`document.devMode`, HTTPS errors ignored, local network allowed).
- To place what a live check found in the Designer, use `wf.py find --text "<the text>"`: the store knows which instance renders it.
- Video, downloads and Webflow's API, form endpoint, dashboard and Designer are blocked in every window.

## Sign-in windows (`hand.py`)

For sites that need the user's own sign-in (not Webflow: the driver's Chrome is already signed in there). The `signin-handoff` skill drives it.

```powershell
python hand.py open https://app.example.com     # a Chrome window on the user's desktop; prints a sign-in verdict
python hand.py status                           # VERDICT: SIGNED IN | NOT YET | UNKNOWN, the tabs, where the window is
python hand.py park                             # after sign-in: move it to the automation desktop (unpark brings it back)
python hand.py run snippet.py                   # Playwright over CDP: page, context, cdp, snap(), check(), progress()
python hand.py close [--forget]                 # --forget deletes the profile (the saved sign-in)
```

- One Chrome per site key, with a persistent profile in `hand-profiles/<key>/` (git-ignored). Same GPU flags as `live.py` from `machine.json`, muted, no background throttling, and the `--aite-hand` switch.
- `watchdog.py` counts `--aite-hand` Chromes as ours and writes `.watchdog.json`. `hand.py` refuses to start (exit 3) when the running watchdog doesn't list that switch.
- `desk.py` leaves these windows on the user's desktop until `park` lists their pid under `parked` in `.hand.json`.
- Exit codes are `x.py`'s: 1 error, 3 stack down, 4 over budget (killed at budget + 10 s), 5 busy.
- A keeper process closes windows after 30 minutes without a `hand.py` command (`hand_idle_s` in `machine.json`).
- Tests: `python tests/test_hand.py`. It opens one window for about a minute and runs against a local fake sign-in page.
