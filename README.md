# Bisect — an autonomous regression hunter

**Paste a public Python repo and a plain-English bug report. Bisect writes a failing test, drives a real
`git bisect` through the history in a locked-down Docker sandbox, names the first bad commit, *proves* that commit
causes the bug today, explains the causal chain, and proposes a fix it verifies against the project's own test suite.**

Built for the **Nebius x NVIDIA Global AI Hackathon 2026, Coding and Agentic Engineering track.**
All inference runs on **Nebius Token Factory** with **NVIDIA Nemotron 3** (Ultra + Nano). MIT licensed.

| | |
|---|---|
| **Demo (verified runs, replayable)** | **https://bisect-agent.pages.dev** — opens the four verified runs instantly; replay any of them |
| **Demo video** | https://youtu.be/tmnFS8Vgka0 |
| **Direct links** | [sqlparse](https://bisect-agent.pages.dev/?run=seed-sqlparse-826) · [pathspec](https://bisect-agent.pages.dev/?run=seed-pathspec-62) · [sql-metadata (dependency bump + Tavily)](https://bisect-agent.pages.dev/?run=seed-sqlmetadata-597) · [Python-Markdown](https://bisect-agent.pages.dev/?run=seed-markdown-1358) |
| **Run your own hunt** | the hosted demo replays recorded runs; live runs need Docker, so run the full app locally (see [Running it](#running-it)) |

---

## Results on real regressions

Four real bugs from real projects, each with a public issue and a maintainer fix. Bisect was given only the bug
report text and a known-good release. It wrote its own reproduction test.

| Seed | Bug (as reported) | Culprit found | Steps / commits | Causal proof | Fix | Time | Model cost |
|---|---|---|---|---|---|---|---|
| [sqlparse #826](https://github.com/andialbrecht/sqlparse/issues/826) | `split()` stops splitting `BEGIN TRANSACTION … END TRANSACTION` scripts | ✅ [`1a3bfbd`](https://github.com/andialbrecht/sqlparse/commit/1a3bfbd50b82c05d9adcc51bacba059671dc1bc7) | 5 / 35 | ✅ (Ultra-resolved revert: a later commit re-wrapped the lines) | ✅ targeted fix; 480/480 suite tests pass | 1.8 min | $0.11 |
| [pathspec #62](https://github.com/cpburnz/python-pathspec/issues/62) | gitignore `*` stops matching files in subdirectories | ✅ [`9eb2908`](https://github.com/cpburnz/python-pathspec/commit/9eb2908bbaecc354dbb62595cca2fd4c6219fd2d) | 5 / 42 | ✅ | ⚠️ fallback revert fixes the bug but re-breaks 2 of 81 tests (the issue the culprit fixed), and the report says so | 3.4 min | $0.17 |
| [sql-metadata #597](https://github.com/macbre/sql-metadata/issues/597) | `CREATE TABLE IF NOT EXISTS foo` reports table `IF NOT EXISTS`, **a pure dependency bump** | ✅ [`fa8eb15`](https://github.com/macbre/sql-metadata/commit/fa8eb1533ce614ce133ddda2551bb6f7ffa131f1) | 6 / 36 | ✅ | ✅ revert of the lock bump; 160/160 suite tests pass. Tavily found the upstream change | 2.1 min | $0.17 |
| [Python-Markdown #1358](https://github.com/Python-Markdown/markdown/issues/1358) | backslashes in raw HTML attributes get mangled | ✅ [`c0f6e5a`](https://github.com/Python-Markdown/markdown/commit/c0f6e5a31ea8e7fe98910a0523144c2a96fa9bf1) | 5 / 37 | ✅ | ✅ fallback revert, verified by the reproduction test (this suite needs the package pip-installed, so it can't run in the sandbox; Bisect detects and reports that) | 14.0 min | $0.22 |

**4/4 culprits correct, 4/4 causally confirmed**, with 69 model calls, 0 errors and $0.67 of inference for all four
hunts. Speculative prefetch had already tested the commit git asked for next on 13 of 21 bisect steps.

Every run is stored with its full event stream: open it to see each commit tested, each verdict, and every model call
with its tier, latency, tokens and cost. You can also replay it.

## What makes Bisect different

1. **Causal proof, not just a first-failing commit.** `git bisect` tells you where a test *started* failing. Bisect
   then reverts **only** that commit on today's HEAD (a real 3-way revert via `git merge-tree`) and reruns the test. A
   pass proves the commit is *why* the bug exists now. When later commits rewrote the same lines (the sqlparse and
   Markdown cases), Nemotron Ultra resolves the revert against current code, and the sandbox, not the model, decides
   the result.
2. **Model routing you can audit.** One table decides which Nemotron tier handles each task. Every call is logged with
   tier, latency, prompt/completion/**thinking** tokens and **dollar cost**, and every run reports what it would have
   cost with Ultra doing everything.
3. **Dependency bumps are bisectable.** Each commit is tested with the exact versions its `poetry.lock` / `uv.lock` /
   `pdm.lock` / `Pipfile.lock` pins. That is how Bisect finds a Dependabot commit that changed nothing but a lock
   file, and then **decides on its own** to research the upstream package with Tavily.
4. **Reproduction that converges.** Ultra writes three candidate tests per attempt. Each attempt runs at HEAD and at the
   known-good commit in parallel, and only candidates that fail with a real assertion now and pass before are kept.
   Repairs see a per-candidate table and the full attempt history.
5. **Self-correcting, verified fixes.** Fixes are exact edits, not hand-written diffs. Each attempt reruns the
   reproduction test and feeds failures back. The full suite then runs on HEAD and HEAD+fix in parallel to catch new
   failures. If no targeted fix passes, the proven revert is offered as a clearly labelled fallback.
6. **Built sandbox-first.** Untrusted code never runs outside an ephemeral, network-less, read-only container. See below.

## How it works

```
bug report + repo URL (+ optional known-good ref; default: 200 commits back)
  │
  ├─ 1. Reproduce   Nano picks the relevant files → Ultra writes 3 candidate tests → sandbox runs them at HEAD
  │                 and known-good in parallel → keep the ones that fail now / passed before (repair, ≤4 attempts)
  ├─ 2. Bisect      real `git bisect --no-checkout` on a bare clone; docs/CI-only commits excluded by pathspec;
  │                 each step = fresh sandbox + Nano verdict (good/bad/skip) + Nano diff summary; the next two
  │                 possible midpoints run speculatively in parallel
  ├─ 3. Prove       revert only the culprit on HEAD (git merge-tree; Ultra resolves conflicts) → rerun the test
  ├─ 4. Research    culprit touches dependency files? Ultra decides if upstream research is needed → Nano plans
  │                 queries → Tavily (changelogs, GitHub issues, CVE databases) → Nano extracts cited facts
  ├─ 5. Explain     Ultra traces the causal chain from lines of the diff to the symptom, citing evidence
  └─ 6. Fix         Ultra writes edits → reproduction test (≤3 attempts, failures fed back) → full suite on HEAD and
                    HEAD+fix in parallel → new failures reported → fallback: the proven revert
```

Hard limits from the spec: **15 bisect steps, 5 minutes per test run, 25 minutes per job**. Hitting one ends the job
with partial findings (the narrowed commit range), not an error.

## How Nemotron is used

All routing lives in [`backend/bisect_agent/router.py`](backend/bisect_agent/router.py); no model name appears
anywhere else.

| Tier | Token Factory model | Thinking | Tasks |
|---|---|---|---|
| **Reason** | `nvidia/Nemotron-3-Ultra-550b-a55b` | on | `write_repro_test`, `repair_repro_test`, `resolve_revert`, `decide_upstream_research`, `explain_culprit`, `propose_fix` |
| **Fast** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | off | `select_context_files`, `classify_verdict`, `summarise_diff`, `filter_candidates`, `plan_search_queries`, `digest_search_results` |

**Why this split.** Ultra handles the few calls where being wrong is expensive. A reproduction test that misses the
bug poisons the whole bisect, and the explanation and fix are the product. Those calls read whole source files and
diffs, so Ultra's long context and reasoning pay for themselves. Nano handles the work that scales with history length:
every bisect step gets a verdict and a diff summary, so Nano makes most of the calls, all on the critical path of the
bisect loop.

**Measured on the four verified runs** (69 calls, 0 errors):

| Tier | Task | Calls | Median latency | Max | Median output tokens | of which thinking |
|---|---|---|---|---|---|---|
| Nano | `classify_verdict` | 21 | 0.9 s | 1.3 s | 35 | 0 |
| Nano | `summarise_diff` | 21 | 1.1 s | 1.5 s | 53 | 0 |
| Nano | `select_context_files` | 4 | 1.4 s | 1.6 s | 70 | 0 |
| Nano | `plan_search_queries` | 1 | 2.1 s | 2.1 s | 179 | 0 |
| Nano | `digest_search_results` | 1 | 5.0 s | 5.0 s | 465 | 0 |
| Ultra | `decide_upstream_research` | 1 | 2.0 s | 2.0 s | 272 | 159 |
| Ultra | `write_repro_test` | 4 | 6.4 s | 17.9 s | 1,836 | 1,177 |
| Ultra | `resolve_revert` | 1 | 4.5 s | 4.5 s | 1,853 | 524 |
| Ultra | `explain_culprit` | 4 | 14.9 s | 40.0 s | 4,643 | 4,203 |
| Ultra | `repair_repro_test` | 1 | 14.9 s | 14.9 s | 4,774 | 4,095 |
| Ultra | `propose_fix` | 10 | 17.4 s | 678 s* | 6,043 | 5,618 |

\* One request stalled before the client streamed responses; see [FRICTION_LOG.md](FRICTION_LOG.md). The client now
streams, detects a stall within 60 s, and bounds every call. Regenerate this table with `python scripts/seed_report.py`.

**Guardrails around the cheap tier.** Hard evidence beats Nano's opinion: a pytest pass is always `good`; a timeout,
collection error or failed install is always `skip`; an ImportError-style failure (the API didn't exist yet) is never
`bad`. Overrides are logged in the step's reason, so the UI shows them.

## Where Token Factory accelerated the workflow

- **One OpenAI-compatible endpoint for both tiers.** Moving a task between Ultra and Nano is a one-line change in the
  routing table, which is how the split was tuned on live runs.
- **Nano at ~1 s per call is what makes a model-in-the-loop bisect viable.** Classification and diff summaries run
  concurrently with the sandbox, so model time is mostly hidden behind test time.
- **Ultra's 1M-token context** fits the culprit diff, the reproduction test, both pytest outputs and the upstream research
  into a single explanation call, with no chunking or retrieval layer.
- **Thinking you can account for.** Token Factory reports `reasoning_tokens` and returns `reasoning_content` separately;
  Bisect records thinking tokens per call and shows them per tier.
- **Cost and latency, measured honestly.** A full hunt costs $0.11–0.22. Routing cuts the bill by only ~12% against
  the same tokens billed at Ultra prices, because Ultra's few long reasoning calls dominate spend (and that figure is a
  lower bound, since Ultra with thinking on would emit more tokens for the same tasks). The bigger win is **latency on
  the critical path**: Nano answers each per-step call in ~1 s, where Ultra with thinking takes 6–15 s. Across a
  15-step bisect that is minutes of waiting removed per hunt.

## Other Nebius services

- **Nebius Token Factory** — all inference. There is no other model provider in the codebase and no fallback:
  [`nebius.py`](backend/bisect_agent/nebius.py) is the only module that calls a model.
- **Nebius Compute** — the live app runs on a Nebius Compute VM (it needs a Docker daemon for the sandboxes, which
  serverless containers don't provide). [`scripts/deploy_vm.sh`](scripts/deploy_vm.sh) deploys or updates it in one command.
- **Nebius Serverless Jobs** — *not used yet.* Each verdict decides the next commit, so the bisect sweep is sequential by
  nature; Bisect parallelises it with speculative midpoints instead. Each sandbox run is already a container image plus
  a command with a timeout, exactly a Serverless Job's shape, so running the sweep there is the next step.

## Tavily: upstream research for dependency regressions

When the first bad commit changes `requirements*.txt`, `pyproject.toml`, `setup.py/cfg` or a lock file, the repo's own
diff often can't explain the break. This is a real branch of the agent's logic:

1. Bisect parses the version changes out of the diff.
2. **Ultra decides** whether the commit's source changes plausibly explain the failure, or whether the cause is upstream.
   A commit that bumps a pin *and* rewrites the parser doesn't need a web search; a Dependabot lock-file bump does.
3. If research is warranted, Nano writes targeted queries per package (changelog / issues / CVE) and Tavily runs them.
   CVE queries are restricted to NVD/OSV/GitHub advisories, issue queries to GitHub.
4. Nano extracts only the relevant facts, each with its source URL; Ultra folds them into the explanation with citations.

The sql-metadata seed shows this end to end: the culprit is `Bump sqlparse from 0.5.3 to 0.5.4`, which changed only
`poetry.lock`. Ultra decided upstream research was needed, Tavily ran 5 searches, and the explanation cites the upstream
issues and security advisory it found. Citations are checked against the URLs Tavily actually returned; anything the
model invents is dropped and recorded.

## The sandbox

Untrusted repository code only ever runs inside [`sandbox.py`](backend/bisect_agent/sandbox.py)'s containers:

- `--network none`, `--read-only` root filesystem, `--cap-drop ALL`, `no-new-privileges`, non-root user, and
  memory/CPU/PID limits.
- The commit is a **read-only bind mount**; tests run on a private tmpfs copy that disappears with the container.
- The hard timeout is enforced twice: `timeout -s KILL` inside, `docker kill` from outside.
- **Dependencies are the only network path, and no repository code runs on it.** A no-network container reads the
  commit's metadata (`pip install --dry-run --no-index --report`) and lock file; a separate networked container that
  does **not** mount the repo installs those exact pins into a content-addressed, cached volume, which test containers
  mount read-only.
- Host-side git never executes repo code: bare clones, hooks disabled, isolated git config, snapshots via a throwaway
  index, reverts via `git merge-tree` in the object store, and edits applied as plain file operations.

`make test` includes isolation tests that try to reach the network and write to the source mount from inside a sandbox.

## Running it

Requirements: Docker, Python 3.11+, Node 20+, a Nebius Token Factory API key and (optionally) a Tavily key.

```bash
git clone https://github.com/sja-thedude/bisect-agent && cd bisect-agent
cp .env.example .env            # add NEBIUS_API_KEY and TAVILY_API_KEY
set -a; source .env; set +a

make setup                      # venv + backend
make sandbox                    # build the sandbox image (other Python versions build on demand)
make models                     # check Token Factory access and ping both Nemotron tiers
make web                        # build the UI
make dev                        # http://localhost:8000
```

Headless, from the CLI:

```bash
cd backend && ../.venv/bin/bisect-agent run \
  --repo https://github.com/andialbrecht/sqlparse \
  --bug "sqlparse.split() no longer splits a BEGIN TRANSACTION; ... END TRANSACTION; script into statements" \
  --good 0.5.3 --bad acd8e5817b5be5f9b9a7b159c1bacc45179f4e44
```

**Tests.** `make test` runs the unit tests, the sandbox isolation tests, and a full offline agent run against a fixture
repo with a fake Token Factory transport (no API key needed). `make verify-sandbox` bisects all four real seeds with
hand-written tests and no AI, which proves the infrastructure on its own. `make seeds` reruns the seeds with real models
and re-exports them to `seeds/runs/`.

### Deploying

- **Full app (live runs):** on any Ubuntu VM with a public IP (we use Nebius Compute), from your laptop:
  `scripts/deploy_vm.sh ubuntu@<vm-ip>`. It installs Docker, checks out the repo, copies your `.env` over SSH, builds
  the sandbox image and starts the app on port 80. Re-run it to update.
- **Static demo (verified runs only):** `python scripts/build_static_demo.py && cd web && NEXT_PUBLIC_STATIC_DEMO=1
  NEXT_PUBLIC_LIVE_URL=<live app url> npm run build` produces `web/out`, which any static host can serve. The recorded
  runs replay in the browser with no backend.

## Limitations (by design or not yet done)

- **Python and pytest only**, as scoped. The package is imported from the checkout (repo root and `src/` on
  `PYTHONPATH`) rather than built, so projects that need compiled extensions at test time are out of scope.
- **Suites that need the package installed** (e.g. extensions found via entry points) can't run their full suite in
  the sandbox. Bisect detects this at untouched HEAD and judges the fix by the reproduction test alone, saying so in
  the report.
- **Fixes are proposals.** When no targeted fix passes, the fallback is the proven revert, which may undo the culprit's
  intended improvement; the report shows exactly which tests that re-breaks.

## Development workflow

Every change goes on a short-lived branch cut from `dev` and merged back with `--no-ff`, so each feature or fix stays
a visible unit in the history (`git log --graph dev`). Several fixes in that history were found by the live seed
runs; their commit messages say what failed and why.

```bash
git checkout dev && git pull
git checkout -b feat/<name>        # or fix/<name>, docs/<name>, chore/<name>
# ...commit...
git checkout dev && git merge --no-ff feat/<name>
make test                          # keep dev green before pushing
git push origin dev feat/<name>
```

## Repository layout

```
backend/bisect_agent/
  router.py     model routing table, cost and thinking-token accounting (the only place model IDs live)
  nebius.py     Token Factory client (the only inference client)
  agent.py      the agent loop and its inspectable JobState
  sandbox.py    Docker sandbox: dependency resolution, install, pytest runs
  gitops.py     bare clones, git bisect driver, speculative midpoints, merge-tree reverts
  tavily.py     Tavily search + dependency-diff parsing
  prompts.py    every prompt, one per routed task
  store.py      SQLite runs + events (permalinks, replay)
  server.py     FastAPI API + SSE + static UI
  cli.py        run / models / serve
sandbox/        sandbox image + in-container dependency resolver
web/            Next.js 15 + Tailwind v4 single-page UI (static export)
seeds/          seed catalog + exported, verified demo runs
scripts/        seed runner, sandbox-only verifier, static demo builder, VM deploy
docs/           demo video script, Devpost write-up
FRICTION_LOG.md notes on Nebius / NVIDIA / Tavily tooling, written as I built
```

## Licence

[MIT](LICENSE)
