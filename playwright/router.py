"""The mode router, phase 1: manual. The user picks the mode with one dedicated word in their message.

    wfasync   one agent, no concurrency: no subagents, one Designer window, one live window
    wfsync    concurrent: up to three Designer windows, live windows up to the machine cap, subagents allowed

    python wf.py mode "<the user's message>" [--previous wfsync|wfasync]

Reading the word is done here, in code, so it is the same every time and can be tested as a table (tests/
test_router.py). No browser. docs/router.md has the design, including the automatic phase that comes later."""
import re

ASYNC, SYNC = "wfasync", "wfsync"
MODES = (ASYNC, SYNC)
DEFAULT = ASYNC  # no word in the message and no earlier choice for this task

# Text the user did not type as an instruction: pasted material, fenced code, quoted lines.
_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content\b[^>]*>", re.S | re.I)  # the closing tag carries the id too
_FENCED = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_QUOTED = re.compile(r"(?m)^\s*>.*$")
# A whole word: not part of a longer name, path or identifier (wfsync.py, my_wfsync, wfsyncing, --wfsync=1 don't count).
_WORD = re.compile(r"(?<![\w./\\-])(wfasync|wfsync)(?![\w/\\-])(?!\.\w)", re.I)


def own_words(message):
    """The message without what the user pasted or quoted: only their own words can choose a mode."""
    text = _PASTED.sub(" ", message or "")
    text = _FENCED.sub(" ", text)
    return _QUOTED.sub(" ", text)


def read_mode(message, previous=None):
    """Which mode this message asks for.

    Returns {"mode": "wfasync" | "wfsync" | None, "source": "word" | "previous" | "default" | "conflict", "note": str}.
    mode None means both words are present: stop and ask the user which one.
    previous: the mode already chosen for this task (a follow-up message without a word keeps it)."""
    found = {m.group(1).lower() for m in _WORD.finditer(own_words(message))}
    if len(found) == 2:
        return {"mode": None, "source": "conflict", "note": "both mode words are in the message: ask the user which one"}
    if found:
        mode = found.pop()
        return {"mode": mode, "source": "word", "note": f"the message says {mode}"}
    if previous in MODES:
        return {"mode": previous, "source": "previous", "note": f"no mode word in this message: keeping {previous} from earlier in the task"}
    return {"mode": DEFAULT, "source": "default", "note": f"no mode word in the message: default {DEFAULT}"}


def obligations(mode):
    """What a mode obliges, for the agent and for the harness."""
    if mode == SYNC:
        return {"mode": SYNC, "subagents": True, "designer_windows": 3, "live_windows": None,
                "text": "concurrent: up to 3 Designer windows, live windows up to the machine cap, subagents allowed for "
                        "independent live, offline and MCP parts (Designer commands go through one agent; never two agents "
                        "writing one page)"}
    return {"mode": ASYNC, "subagents": False, "designer_windows": 1, "live_windows": 1,
            "text": "one agent, no concurrency: no subagents, one Designer window, one live window"}


def windows_for(mode, asked=None, kind="designer"):
    """The window count a command may use under a mode. asked: the --windows the caller gave, if any.
    Returns (windows or None for the tool's own default, note or None when the request was capped)."""
    cap = obligations(mode)["designer_windows" if kind == "designer" else "live_windows"]
    if mode == ASYNC:
        return 1, (f"{ASYNC}: --windows {asked} ignored, one window" if asked and int(asked) != 1 else None)
    if asked is None:
        return None, None
    asked = int(asked)
    if cap and asked > cap:
        return cap, f"{SYNC}: --windows {asked} capped at {cap}"
    return asked, None
