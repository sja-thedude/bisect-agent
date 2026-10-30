# Devpost submission — copy/paste material

**Project name:** Bisect

**Tagline (≤ 200 chars):** An autonomous regression hunter: from a plain-English bug report to a proven culprit commit
and a verified fix, with NVIDIA Nemotron on Nebius Token Factory.

**Track:** Coding and Agentic Engineering

**Links:**
- Demo: https://bisect-agent.pages.dev
- Code: https://github.com/sja-thedude/bisect-agent (MIT)
- Video: https://youtu.be/tmnFS8Vgka0

---

## Inspiration
"It worked in 2.1" is one of the most common bug reports, and one of the most tedious to chase. Finding the commit means
writing a reproduction, checking out commits, running tests, and squinting at diffs. `git bisect` automates the binary
search but not the judgement around it: what to test, whether a failure is the bug or noise, why the commit broke it,
and how to fix it. That judgement is exactly what a reasoning model is good at, so long as it's grounded in real test runs.

## What it does
Give Bisect a public Python repo and a bug report. It then:
1. **Reproduces.** Nemotron 3 Ultra writes three candidate pytest tests. Bisect runs them at HEAD and at the known-good
   commit in parallel, in a network-less, read-only Docker sandbox, and keeps the one that fails now and passed before.
2. **Bisects.** It drives real `git bisect`. Every commit runs in a fresh sandbox, and Nemotron Nano classifies each pytest
   result as good, bad or untestable in about a second. The next two possible midpoints are tested speculatively in
   parallel.
3. **Proves causation.** It reverts only the culprit on today's HEAD and reruns the test. If git's revert conflicts with
   later edits, Ultra resolves the revert and the sandbox decides. This turns "where the test started failing" into
   "why it fails today".
4. **Researches upstream.** When the culprit is a dependency bump, Ultra decides whether the repo's own diff can explain
   the break. If it can't, Tavily searches the package's changelog, issues and CVE databases.
5. **Explains and fixes.** Ultra traces the causal chain from specific lines of the diff to the symptom and writes a fix
   as exact edits. Bisect reruns the reproduction test (feeding failures back, up to 3 attempts) and then the project's
   full test suite, reporting any new failures.

Everything streams live: a timeline of commits narrowing with each verdict, every model call with its tier, latency,
tokens and cost, and a final report you can copy as a GitHub issue.

## How we built it
- **Model routing as a first-class module.** One table maps every task to a tier. Ultra (thinking on) handles the 5–8
  high-value calls per job; Nano (thinking off) handles the dozens of per-commit calls. Each run reports its actual cost
  against the same tokens billed at Ultra prices.
- **Sandbox first.** It was built before any AI. Every container gets `--network none`, a read-only mount of the commit,
  dropped capabilities, a non-root user and hard timeouts. Dependencies are the only network path: a no-network
  container reads the commit's metadata and lock file, and a separate container that never sees repo code installs them.
- **Lock-file aware.** Each commit is tested with the dependency versions its `poetry.lock`/`uv.lock`/`pdm.lock` pins.
  That is what makes a Dependabot-bump regression findable at all.
- **Guardrails over the cheap tier.** Hard evidence (a pass, a timeout, an ImportError) overrides Nano's verdict, and
  every override is logged.
- Python + FastAPI + SQLite + Docker on the backend, Next.js 15 + Tailwind v4 on the frontend, SSE for the live stream.

## Verified on real regressions
Given only the bug report text and a known-good release, Bisect found the correct culprit in all four cases, each one
matching the maintainers' own fix history, and causally confirmed all four by reverting the culprit on HEAD.

| Repo | Bug | Culprit | Steps | Causal proof | Fix |
|---|---|---|---|---|---|
| sqlparse #826 | `split()` stops splitting `BEGIN TRANSACTION` | ✅ `1a3bfbd` | 5 of 35 commits | ✅ | ✅ targeted fix, 480/480 suite tests |
| pathspec #62 | `*` stops matching files in subdirectories | ✅ `9eb2908` | 5 of 42 | ✅ | ⚠️ revert re-breaks 2 tests (reported) |
| sql-metadata #597 | `CREATE TABLE IF NOT EXISTS` reports table "IF NOT EXISTS" (pure dependency bump) | ✅ `fa8eb15` | 6 of 36 | ✅ | ✅ 160/160 suite tests; Tavily found the upstream change |
| Python-Markdown #1358 | backslashes in raw HTML attributes mangled | ✅ `c0f6e5a` | 5 of 37 | ✅ | ✅ verified by the reproduction test |

69 model calls, 0 errors, $0.67 of inference for all four hunts (1.8–14 minutes each).

## Challenges
- **Code moves after the culprit.** On sqlparse, a later "Code cleanup" commit re-wrapped the culprit's lines. That broke
  both the model's search/replace edits and `git revert`. We added a whitespace- and comment-insensitive edit locator
  and model-resolved reverts that the sandbox verifies.
- **Reproduction tests that oscillate.** A single test would fail at the old release for incidental reasons, then get
  "repaired" into one that passed at HEAD. Three candidates per attempt, judged at both ends, plus the full attempt
  history fixed it.
- **Making a dependency bump bisectable.** Pinning each commit's lock-file versions was the difference between finding
  the Dependabot commit and blaming the known-good release.

## What's next
Running sandbox sweeps on Nebius Serverless Jobs for wider parallel bisection, more languages and test runners, and a
GitHub App that answers "this used to work" issues with a ready-to-merge PR.

## Built with
nvidia-nemotron, nebius-token-factory, tavily, python, fastapi, docker, git, pytest, next.js, tailwindcss, sqlite

---

# Feedback forms

## Nebius Token Factory
- **What worked:** exact catalog IDs worked first time; `/v1/models` made a preflight check trivial; hundreds of calls
  with no errors and no 429s; JSON mode was reliable on Nemotron; `reasoning_tokens`, `reasoning_content`,
  `x-request-id` and the dynamic rate-limit headers are all there.
- **What to improve:**
  1. Document `chat_template_kwargs` (the Nemotron thinking toggle). I found it only in NVIDIA's HF cards.
  2. Document that reasoning comes back in `reasoning_content` and is counted in `completion_tokens_details`.
  3. Offer a server-side thinking budget. One Ultra repair call produced 19k completion tokens in 66 s.
  4. Document streaming and client timeouts for long reasoning calls. One non-streamed Ultra call took 678 s, most
     likely two 300 s client timeouts on a stalled generation.
  5. The 402 "credit balance exhausted" response is clear, but we didn't see any warning before it. A low-balance
     response header would let a client warn before a long batch dies mid-run.
  6. Fix the JSON-mode docs: the prose and the schema disagree, and the Python example has syntax errors.
  7. Retire removed model IDs from the doc snippets, and say whether `api.studio.nebius.com` is an alias.
  8. Use consistent model-ID casing across the family.

## Nebius AI Cloud
<fill in after deploying the demo VM: what was smooth, what wasn't>

## NVIDIA Nemotron
- Ultra's long context fit a whole culprit diff, both pytest outputs and the upstream research into one explanation call.
- Nano with thinking off was consistently ~1 s for classification, fast enough to sit inside the bisect loop.
- Nano sometimes misread a *passing* pytest run as matching the reference failure when the prompt led with the failure
  output. Leading with the pass/fail result fixed it, and hard-evidence guardrails stay in place regardless.
- Ultra occasionally copied edit snippets from an older diff rather than the current file, and sometimes assembled
  plausible but non-existent URLs (e.g. a dbt issue number on the sqlparse repo). We verify edits against the file and
  citations against the retrieved search results.

## Tavily
- `include_domains` scoping (NVD/OSV/GitHub for CVEs, GitHub for issues) noticeably improved relevance.
- A citations-in-answer option would let us skip the extra digest call we use to keep a URL attached to every fact.
