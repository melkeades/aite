"""router.py, phase 1 (manual mode): reading the mode word and capping the windows. Every row must pass: with the word
given, the mode is exactly that word's mode. No browser, runs in a second.

    python tests/test_router.py"""
import os, sys

PW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PW)
import router  # noqa: E402

A, S = router.ASYNC, router.SYNC
results = []


def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(info)[:200]}]" if info and not ok else ""))


# (name, message, previous, expected mode, expected source)
TABLE = [
    # the word, wherever the user put it
    ("word at the start", "wfsync go through every page and fix the cards", None, S, "word"),
    ("word at the end", "set prop P on /pricing wfasync", None, A, "word"),
    ("word in the middle", "please, wfsync, audit the whole site", None, S, "word"),
    ("word in capitals", "WFSYNC audit the site", None, S, "word"),
    ("word in mixed case", "WfAsync change the heading on /about", None, A, "word"),
    ("word with punctuation after it", "wfasync: change the heading", None, A, "word"),
    ("word in brackets", "(wfsync) check all service pages", None, S, "word"),
    ("word in inline backticks", "`wfsync` check all service pages", None, S, "word"),
    ("word on its own line", "fix the footer links\nwfasync", None, A, "word"),
    ("word followed by a full stop", "Do it with wfsync.", None, S, "word"),
    ("word followed by a question mark", "can you run this wfasync?", None, A, "word"),
    # no word
    ("no word: default", "change the heading on /about", None, A, "default"),
    ("no word, empty message", "", None, A, "default"),
    ("no word, earlier wfsync kept", "now do the blog pages too", S, S, "previous"),
    ("no word, earlier wfasync kept", "and the footer", A, A, "previous"),
    ("a word overrides the earlier choice", "wfasync just fix this one button", S, A, "word"),
    ("an unknown earlier value is ignored", "do it", "turbo", A, "default"),
    # lookalikes that must not count
    ("plain 'sync' is not a mode word", "sync the pages and tell me what changed", None, A, "default"),
    ("plain 'async' is not a mode word", "is the async function in main.js broken?", None, A, "default"),
    ("wf.py sync command text", "run python wf.py sync ca-nefi /blog", None, A, "default"),
    ("part of a longer word", "the wfsyncing step is slow", None, A, "default"),
    ("part of an identifier", "rename my_wfsync_flag in the config", None, A, "default"),
    ("a file name", "open wfsync.py and read it", None, A, "default"),
    ("a path", "look in docs/wfsync/notes.md", None, A, "default"),
    ("a flag", "what does --wfsync do?", None, A, "default"),
    ("a hyphenated name", "the wfsync-runner package", None, A, "default"),
    # words the user did not type as an instruction
    ("only inside pasted content", "look at this log <pasted_content id=\"x1\">agent said wfsync here</pasted_content id=\"x1\"> why did it fail", None, A, "default"),
    ("only inside a fenced block", "what does this do?\n```\nmode = 'wfsync'\n```", None, A, "default"),
    ("only inside a tilde fence", "~~~\nwfasync\n~~~\nexplain", S, S, "previous"),
    ("only in a quoted line", "> use wfsync for this\nis that right?", None, A, "default"),
    ("own word wins over pasted other word", "wfasync <pasted_content id=\"q\">wfsync wfsync</pasted_content id=\"q\"> fix the title", None, A, "word"),
    ("own word plus a fenced other word", "wfsync run this\n```\nwfasync\n```", None, S, "word"),
    # both
    ("both words: ask", "wfsync or wfasync, whichever is faster", None, None, "conflict"),
    ("both words, with an earlier choice: still ask", "wfasync wfsync", S, None, "conflict"),
    ("the same word twice is one word", "wfsync, I said wfsync", None, S, "word"),
]
for name, msg, prev, mode, source in TABLE:
    r = router.read_mode(msg, prev)
    check(name, r["mode"] == mode and r["source"] == source, r)

# window caps
W = [
    ("wfasync: one Designer window by default", (A, None, "designer"), (1, False)),
    ("wfasync: --windows 3 is ignored", (A, 3, "designer"), (1, True)),
    ("wfasync: --windows 1 is fine", (A, 1, "designer"), (1, False)),
    ("wfasync: one live window", (A, None, "live"), (1, False)),
    ("wfasync: live --windows 4 is ignored", (A, 4, "live"), (1, True)),
    ("wfsync: the tool's own default", (S, None, "designer"), (None, False)),
    ("wfsync: --windows 2 kept", (S, 2, "designer"), (2, False)),
    ("wfsync: Designer capped at 3", (S, 5, "designer"), (3, True)),
    ("wfsync: live --windows 4 kept", (S, 4, "live"), (4, False)),
]
for name, args, (want, noted) in W:
    n, note = router.windows_for(*args)
    check(name, n == want and bool(note) == noted, (n, note))

check("wfasync forbids subagents", router.obligations(A)["subagents"] is False)
check("wfsync allows subagents", router.obligations(S)["subagents"] is True)

bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)} of {len(results)} passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
