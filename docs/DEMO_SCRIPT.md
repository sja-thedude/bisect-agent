# Demo video script (target 2:45, hard limit 3:00)

Record on a Mac with QuickTime (File → New Screen Recording) or ⌘⇧5, 1440×900 or larger, browser zoom 100%,
dark UI. Speak slowly; the cuts below are where you pause. Upload to YouTube as **Public** (Unlisted is not enough).

**Before recording**
1. Open the demo URL in a fresh Chrome window (no bookmarks bar, close other tabs).
2. Have three tabs ready:
   - Tab 1: the home page.
   - Tab 2: `/?run=seed-sqlparse-826`
   - Tab 3: `/?run=seed-sqlmetadata-597`
3. If the live app is up, have a live run ready to start (see 0:20). If not, use **▶ Replay run** on a seed instead;
   say "this is a recorded run replaying" so it's honest.

---

### 0:00–0:20 — The problem (home page)
> "Every engineer has done this: a bug report says 'it used to work', and you spend an afternoon running git bisect by
> hand. Bisect does the whole hunt autonomously. You give it a public Python repo and a bug report in plain English."

*Show the home page: headline, the form, the two-tier routing card.*

### 0:20–0:45 — Start a run (Tab 2, press ▶ Replay run, or start a live run)
> "Here's a real regression in sqlparse. The report just says split() stopped splitting BEGIN TRANSACTION scripts.
> First, Nemotron 3 Ultra reads the report and the source, and writes three candidate pytest tests. Bisect runs
> them in a locked-down Docker sandbox, with no network and a read-only checkout, at HEAD and at the last good release in
> parallel, and keeps the one that fails now and passed before."

*Point at the Reproduction test panel when it appears.*

### 0:45–1:15 — The bisect (timeline + steps table)
> "Now it drives real git bisect. Each commit is tested in a fresh sandbox, and Nemotron Nano classifies the result
> in about a second. Nano is the cheap tier, Ultra the reasoning tier, and every call is logged with its tier,
> latency, tokens and cost. Watch the timeline: each verdict halves the search window. Bisect also tests the next
> two possible midpoints in parallel. That's the 'prefetched' tag."

*Point at the timeline strip, a "prefetched" tag and the Model routing panel.*

### 1:15–1:45 — Culprit + causal proof (report card)
> "Five steps, and it has the first bad commit. Most tools stop there. Bisect then *proves* causation: it reverts only
> that commit on today's code and reruns the test. Here a later commit reformatted the same lines, so git's revert
> conflicted, and Ultra resolved the revert while the sandbox decided. The test passes, so this commit is why the
> bug exists today."

*Point at the green "causal proof ✓" callout, then the causal chain.*

### 1:45–2:05 — The fix
> "Ultra traces the causal chain through the diff and writes a fix. Bisect applies it, reruns the reproduction test,
> and runs the project's full test suite: 481 tests, zero new failures. One click turns all of this into a GitHub
> issue."

*Scroll to the fix diff and the three result boxes; click "Copy as GitHub issue".*

### 2:05–2:35 — Dependency bump + Tavily (Tab 3)
> "Sometimes the code didn't change at all. Here the culprit is a Dependabot commit that only touched poetry.lock.
> Bisect installs each commit's locked versions, so it can find that. Ultra then decides on its own that the
> repo's history can't explain the break, and Tavily searches the upstream changelog, issues and CVE databases. The
> explanation cites the sqlparse release that changed the behaviour."

*Point at the purple "Upstream research · Tavily" panel and its sources.*

### 2:35–2:50 — Close (Model routing panel)
> "All inference runs on Nebius Token Factory with NVIDIA Nemotron: Ultra for the few calls that need reasoning, Nano
> for the many that need speed. This whole hunt cost about six cents. Bisect: from bug report to a proven culprit and a
> verified fix, autonomously."

*End on the run-cost box.*

---

**Numbers to say** (from the verified runs; update if you re-run): sqlparse found in 5 bisect steps; fix verified across
481 tests; sql-metadata run cost about $0.06. Check the run pages before recording and use what they show.
