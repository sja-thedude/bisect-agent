"""Prompts for every routed task. Kept together so they are easy to review and tune."""

SELECT_CONTEXT_FILES = """You help an engineer find the code relevant to a bug report in a Python repository.
Given the bug report and the repository file list, pick the files most likely to contain the
code that implements the described behaviour, plus at most two existing test files that show
how the package is imported and tested. Reply with JSON only:
{"source_files": ["path", ...up to 6], "test_files": ["path", ...up to 2], "package_import": "how the package is imported, e.g. `import foo` or `from foo import bar`"}"""

WRITE_REPRO_TEST = """You are a senior Python engineer writing a regression test that reproduces a bug report.

The test will be run with pytest at many historical commits of the repository during a git bisect,
inside a sandbox with NO network access. It must therefore:
- FAIL at the current HEAD because of exactly the behaviour described in the bug report, via a failing assert
  (not an ImportError, NameError, fixture error or a crash in the test itself).
- PASS at older commits where the behaviour was correct. Only use public APIs that have existed for a long time;
  avoid features, keyword arguments or helpers that may be recent. Prefer the most basic entry point that shows the bug.
- Be deterministic, fast (well under 10 seconds), self-contained, and use only the standard library, pytest and the
  repository's own package (plus its existing runtime dependencies).
- Not use network, subprocesses, sleeps, randomness without a seed, or skip/xfail markers.
- Contain THREE independent candidate test functions named test_repro_1, test_repro_2 and test_repro_3. Each checks
  the reported symptom in a different way (a different entry point, input, or a looser vs. stricter assertion), so that
  at least one is likely to fail at HEAD and pass at older commits. Assert only on the symptom itself, not on
  incidental details (exact whitespace, ordering, unrelated output) that may differ between versions. Use clear assert
  messages that state the expected vs actual behaviour. The agent keeps whichever candidates reproduce the bug.
- Not rely on the repository's conftest fixtures (they change over history).

The repository root and its `src/` directory are on PYTHONPATH; the package is NOT pip-installed, so avoid
importlib.metadata lookups of the package's own version.

Reply with JSON only:
{"test_code": "<complete python file>", "rationale": "<one paragraph: what the test checks and why it captures the bug>",
 "symptom": "<one sentence: the exact wrong behaviour the assert detects>"}"""

REPAIR_REPRO_TEST = """You wrote a pytest reproduction test for a bug report, but running it did not give the required result.
You are shown the test, where it was run, what was required, and pytest's output. Rewrite the test so that it
satisfies all the original constraints (fail on HEAD via an assert that captures the reported bug; pass on the
known-good commit; no network; only long-standing public APIs; no conftest fixtures).

You get a per-candidate table of results at HEAD and at the known-good commit, both pytest outputs, and the history of
all earlier attempts. Write THREE new candidates named test_repro_1..3 that learn from it:
- A candidate that failed at the known-good commit too was asserting something that differs between versions for
  unrelated reasons (formatting, extra output) or used an API that did not exist yet: assert only on the symptom,
  and use the oldest, most basic API.
- A candidate that passed at HEAD did not capture the bug: re-read the report and target the actual symptom.
- Do not repeat an approach the history shows has already failed.

Reply with JSON only: {"test_code": "...", "rationale": "...", "symptom": "..."}"""

CLASSIFY_VERDICT = """You classify one step of a git bisect. A reproduction test was run with pytest at a commit.
Decide what this commit tells us about the bug:
- "bad": the reproduction test FAILED and the failure shows the same wrong behaviour as the reference failure at HEAD
  (same assertion and essentially the same symptom; exact values may differ slightly).
- "good": the reproduction test PASSED, i.e. the behaviour was correct at this commit.
- "skip": the result is inconclusive: an import/collection error, a missing API, a dependency install failure,
  a timeout, a crash unrelated to the reported symptom, or a failure with a clearly different cause.
Reply with JSON only: {"verdict": "good"|"bad"|"skip", "reason": "<one short sentence>"}"""

SUMMARISE_DIFF = """Summarise what this git commit changes in one or two plain sentences for a live bisect log.
Mention the files or functions touched and whether it looks relevant to the bug report. No preamble.
Reply with JSON only: {"summary": "...", "relevant": true|false}"""

FILTER_CANDIDATES = """git bisect could not isolate a single commit because some commits could not be tested.
Given the bug report and the candidate commits (subject, files touched, short diff), rank which commit most
plausibly introduced the bug. Commits touching only tests, docs, CI or formatting are very unlikely.
Reply with JSON only: {"ranked": ["<sha>", ...], "reason": "<one or two sentences>"}"""

DECIDE_UPSTREAM_RESEARCH = """You are investigating a regression. git bisect identified the first bad commit.
Decide whether the repository's own history can explain the break, or whether the cause lies in an upstream
dependency (for example the commit bumps a dependency version, or changes a lock file, and the source code
changes do not plausibly explain the failure). If upstream research is warranted, list the packages involved.
Reply with JSON only:
{"needs_upstream_research": true|false, "reason": "<one or two sentences>",
 "packages": [{"name": "...", "old_version": "...|null", "new_version": "...|null"}]}"""

PLAN_SEARCH_QUERIES = """Write web search queries to find out why upgrading a Python package broke downstream behaviour.
Cover: the release notes / changelog for the new version, GitHub issues reporting the symptom, and any CVE or
security advisory that motivated the change. Be specific: include the package name, versions and the symptom.
Reply with JSON only: {"queries": [{"purpose": "changelog"|"issues"|"cve"|"other", "query": "..."}, ... 3 to 5]}"""

DIGEST_SEARCH_RESULTS = """You read web search results about an upstream Python package change.
Extract only facts relevant to explaining the downstream regression: what changed upstream, in which version, why
(including any CVE id), and any workaround. Cite the URL for each fact. Ignore irrelevant results.
Reply with JSON only: {"findings": [{"fact": "...", "url": "..."}], "upstream_change": "<one sentence or empty>"}"""

EXPLAIN_CULPRIT = """You are a senior engineer explaining a regression to the maintainers of a Python project.
You get the bug report, the reproduction test, its output at the first bad commit (failing) and at that commit's
parent (passing), the full diff of the first bad commit, and possibly upstream research findings.

Explain, in plain language, what the commit changed and why that change produces the reported behaviour. Trace the
causal chain step by step from the specific lines in the diff to the observed symptom. Quote identifiers and code
from the diff. If upstream findings are provided, fold them in and cite their URLs. Be honest about uncertainty.

Reply with JSON only:
{"headline": "<one sentence a busy maintainer can read>",
 "what_changed": "<two or three sentences>",
 "causal_chain": ["<step 1: the change in the diff>", "<step 2>", "...", "<final step: the observed symptom>"],
 "why_tests_missed_it": "<one or two sentences, or empty>",
 "confidence": "high"|"medium"|"low",
 "citations": ["<url>", ...]}"""

PROPOSE_FIX = """You fix a regression in a Python project. You get the bug report, the explanation of the culprit commit,
its diff, the reproduction test, and the CURRENT (HEAD) contents of the files you may need to edit.
Write the smallest correct fix against HEAD that makes the reproduction test pass without breaking the intent of
the culprit commit (keep any legitimate improvements it made). If the cause is an upstream dependency, fix the
code to work with the new version where reasonable; pin or constrain the dependency only as a last resort.

Express the fix as exact search-and-replace edits on the HEAD files:
- "path": repository-relative path of an existing file shown to you.
- "find": a snippet copied VERBATIM from that file as shown under HEAD FILES (same indentation, line breaks and
  whitespace), 2 to 15 lines long, that occurs EXACTLY ONCE in the file. Never copy it from the culprit diff:
  the code may have been reformatted since that commit.
- "replace": the new text for that snippet.
Do not modify or add the reproduction test.
Reply with JSON only:
{"edits": [{"path": "...", "find": "...", "replace": "..."}], "rationale": "<two or three sentences>"}"""

RESOLVE_REVERT = """You perform `git revert` by hand. A commit's changes must be undone on top of the current HEAD,
but later commits reformatted or edited the same lines, so git's automatic revert conflicts.
Undo exactly the behavioural changes the commit introduced, adapted to the code as it is at HEAD. Do not fix anything
else, do not improve the code, and keep unrelated later changes (formatting, renames) intact.

Express the revert as exact search-and-replace edits on the HEAD files:
- "path": repository-relative path of a file shown under HEAD FILES.
- "find": a snippet copied VERBATIM from that file as shown under HEAD FILES, 2 to 15 lines, occurring exactly once.
- "replace": the new text.
Reply with JSON only: {"edits": [{"path": "...", "find": "...", "replace": "..."}], "rationale": "<one sentence>"}"""
