# Bisect — an autonomous regression hunter

**Paste a public Python repo and a plain-English bug report. Bisect writes a failing test that reproduces
the bug, drives a real `git bisect` through the history in a locked-down Docker sandbox, names the first
bad commit, explains how that commit causes the symptom, and proposes a fix that it verifies against the
full test suite.**

Built for the **Nebius x NVIDIA Global AI Hackathon 2026, Coding and Agentic Engineering track**.
All inference runs on **Nebius Token Factory** using **NVIDIA Nemotron 3** models. MIT licensed.

- **Live demo:** TODO — demo URL (Nebius Compute VM)
- **Demo video:** TODO
- **Verified demo runs:** open from the demo home page; each has a permalink like `/?run=seed-sqlparse-826`

---

## What it does

```
bug report + repo URL
      │
      ▼
 1. Reproduce   Nemotron 3 Ultra reads the report and the relevant source, writes a pytest that should
                fail on HEAD. Bisect runs it in the sandbox at HEAD (must FAIL with an assertion) and at
                the known-good ref (must PASS). If either check fails, Ultra gets the pytest output and
                rewrites the test (up to 4 attempts).
 2. Bisect      Real `git bisect --no-checkout` on a bare clone. Each commit git asks about is tested in a
                fresh sandbox container; Nemotron 3 Nano classifies the pytest output as good/bad/skip.
                The next two possible midpoints are tested speculatively in parallel.
 3. Research    If the first bad commit touches dependency files, Ultra decides whether the repo's own
                diff explains the break. If not, Nano plans searches and Tavily queries upstream
                changelogs, release notes, GitHub issues and CVE databases.
 4. Explain     Ultra traces the causal chain from specific lines of the culprit diff to the symptom,
                folding in upstream findings with citations.
 5. Fix         Ultra writes a unified diff against HEAD. Bisect applies it and runs the full suite on
                HEAD and HEAD+fix in parallel sandboxes, reporting new failures (if any).
```

Every step streams live to the web UI: each commit tested, its verdict, which model tier classified
it, how long the test took, and every model call with tier, latency and token count.

## How Nemotron is used

Bisect uses **two tiers of NVIDIA Nemotron 3**, chosen in exactly one place —
[`backend/bisect_agent/router.py`](backend/bisect_agent/router.py). No model name appears anywhere else.

| Tier | Model (Token Factory ID) | Thinking | Tasks |
|---|---|---|---|
| **Reason** | `nvidia/Nemotron-3-Ultra-550b-a55b` | on | `write_repro_test`, `repair_repro_test`, `decide_upstream_research`, `explain_culprit`, `propose_fix` |
| **Fast** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | off | `classify_verdict`, `summarise_diff`, `select_context_files`, `filter_candidates`, `plan_search_queries`, `digest_search_results` |

**Why this split.** The reasoning tier does the work where being wrong is expensive and the call count is
small (≈3–8 per job): a reproduction test that doesn't capture the bug poisons the whole bisect, and the
explanation and fix are the product. These are long-context reads of source files and diffs, so Ultra's
1M context and reasoning earn their cost. The fast tier does the work that scales with history length:
every bisect step gets a verdict classification and a diff summary, so a job makes 2× as many fast calls
as it tests commits, and those calls sit on the critical path of the bisect loop. Nano with thinking off
is what keeps a step's model overhead at around a second instead of dominating the wall clock.

**Guardrails around the cheap tier.** Nano's verdicts are checked against hard evidence: a pytest pass is
always `good`; a timeout, collection error or failed install is always `skip`; a failure that is an
ImportError/NameError (the API didn't exist yet) is never allowed to be `bad`. Overrides are logged in the
step's reason, so the UI shows when it happened.

Every call is recorded as a `CallRecord` (task, tier, model, latency, prompt/completion tokens, the
commit it was about) and streamed to the UI. The run page shows per-tier totals and the full call log.

## Where Token Factory accelerated the workflow

- **One OpenAI-compatible endpoint for both tiers.** Switching a task between Ultra and Nano is a
  one-line change in the routing table, which let me tune the split empirically.
- **Nano's latency is what makes model-in-the-loop bisect viable.** TODO: measured median latency per tier
  from the seed runs.
- **Ultra's 1M context** means the culprit diff, the reproduction test, both pytest outputs and the
  upstream research fit in a single explanation call with no chunking or retrieval layer.
- **Speculative parallelism.** Because model calls are fast and run concurrently with sandbox runs, Bisect
  tests the next two possible midpoints while the current commit runs; seed runs show `prefetched` hits.

Token usage per seed run: TODO (filled from `model_totals` of the seed runs).

## Other Nebius services

- **Nebius Token Factory** — all inference (see above). There is no other provider in the codebase and no
  fallback: [`nebius.py`](backend/bisect_agent/nebius.py) is the only module that calls a model.
- **Nebius Compute** — TODO: the hosted demo runs on a Nebius Compute VM (it needs a Docker daemon for the
  sandbox, which serverless containers don't provide).
- **Nebius Serverless Jobs** — not used yet. The bisect sweep is sequential by nature (each verdict decides
  the next commit); Bisect parallelises it locally via speculative midpoints. Sandbox runs are a natural fit
  for Serverless Jobs (each is a container image + command with a timeout), and the sandbox runner is a
  single method, so that is the next step.

## Tavily: upstream research for dependency regressions

When the first bad commit changes `requirements*.txt`, `pyproject.toml`, `setup.py/cfg` or a lock file
(`poetry.lock`, `uv.lock`, `pdm.lock`, `Pipfile.lock`), the repo's own diff often can't explain the break.
The agent handles this as a real branch:

1. Bisect parses the version changes out of the diff.
2. **Ultra decides** (`decide_upstream_research`) whether the source changes in the commit plausibly explain
   the failure, or whether the cause is upstream. A commit that bumps a pin *and* rewrites the parser
   usually doesn't need a web search; a Dependabot lock-file bump does.
3. If research is warranted, Nano writes targeted queries per package (changelog / issues / CVE) and
   **Tavily** runs them, with CVE queries restricted to NVD/OSV/GitHub advisories and issue queries to GitHub.
4. Nano extracts the relevant facts with source URLs; Ultra folds them into the explanation and cites them.

The sql-metadata seed shows this end to end: the regression is a Dependabot commit that only changes
`poetry.lock` (sqlparse 0.5.3 → 0.5.4).

To make that seed bisectable at all, the sandbox installs **the dependency versions each commit's lock file
pins** (as pip constraints). Without this, every commit would be tested against today's sqlparse and the
known-good ref would look bad.

## The sandbox

Untrusted repository code only ever runs inside [`sandbox.py`](backend/bisect_agent/sandbox.py)'s containers:

- `--network none`, `--read-only` root filesystem, `--cap-drop ALL`, `no-new-privileges`, non-root user,
  memory/CPU/PID limits.
- The commit is a **read-only bind mount** (`/src:ro`); tests run on a private tmpfs copy that disappears
  with the container.
- Hard timeout enforced twice: `timeout -s KILL` inside, `docker kill` from outside.
- **Dependencies are the only network path, and no repo code runs on it.** A no-network container reads the
  commit's metadata (`pip install --dry-run --no-index --report`) and lock file to produce requirement
  strings; a separate networked container that does **not** mount the repo pip-installs them into a
  content-addressed, cached volume that test containers mount read-only.
- Host-side git never executes repo code: bare clones, hooks disabled, isolated git config, snapshots via a
  throwaway index + `checkout-index`.

Limits (spec): max **15 bisect steps**, **5 minutes per test run**, **25 minutes per job**, default range
of the **last 200 commits** when no known-good ref is given. Hitting a limit ends the job with partial
findings (the narrowed commit range) instead of an error.

## Verified demo seeds

Each seed is a real regression with a public issue and a maintainer fix. The good/bad refs and expected
culprit are in [`seeds/catalog.json`](seeds/catalog.json).

| Seed | Bug report (abridged) | Expected culprit | Sandbox-only bisect | Full agent run |
|---|---|---|---|---|
| [sqlparse #826](https://github.com/andialbrecht/sqlparse/issues/826) | `split()` returns one statement for a `BEGIN TRANSACTION … END TRANSACTION` script | `1a3bfbd` Fix handling of semicolons inside BEGIN...END blocks | ✅ 5 steps | TODO |
| [pathspec #62](https://github.com/cpburnz/python-pathspec/issues/62) | gitignore `*` stops matching files in subdirectories | `9eb2908` Handle case for issue 19 | ✅ 5 steps | TODO |
| [sql-metadata #597](https://github.com/macbre/sql-metadata/issues/597) | `CREATE TABLE IF NOT EXISTS foo` reports table `IF NOT EXISTS` (**dependency bump**) | `fa8eb15` Bump sqlparse from 0.5.3 to 0.5.4 | ✅ 6 steps | TODO |
| [Python-Markdown #1358](https://github.com/Python-Markdown/markdown/issues/1358) | backslashes in raw HTML attributes get mangled | `c0f6e5a` Move backslash unescaping to treeprocessor | ✅ 5 steps | TODO |

"Sandbox-only bisect" is `make verify-sandbox`: a hand-written reproduction test, the real sandbox and the
real bisect driver, no AI. It proves the infrastructure finds each culprit. "Full agent run" is
`make seeds`: Nemotron writes the test from the bug report alone and classifies every step.

## Running it

Requirements: Docker, Python 3.11+, Node 20+, a Nebius Token Factory API key, and (optionally) a Tavily key.

```bash
git clone https://github.com/sja-thedude/bisect-agent && cd bisect-agent
cp .env.example .env            # add NEBIUS_API_KEY (and TAVILY_API_KEY)
set -a; source .env; set +a

make setup                      # venv + backend
make sandbox                    # build the sandbox image (other Python versions build on demand)
make models                     # check Token Factory access and ping both Nemotron tiers
make web                        # build the UI
make dev                        # http://localhost:8000
```

Headless, from the CLI:

```bash
cd backend
../.venv/bin/bisect-agent run \
  --repo https://github.com/andialbrecht/sqlparse \
  --bug "sqlparse.split() no longer splits a BEGIN TRANSACTION; ... END TRANSACTION; script into statements" \
  --good 0.5.3 --bad acd8e5817b5be5f9b9a7b159c1bacc45179f4e44
```

Tests: `make test` (unit tests, sandbox isolation tests, and a full offline agent run against a fixture
repo with a fake Token Factory transport — no API key needed). `make verify-sandbox` bisects the real seeds
without AI. `make seeds` re-runs the seeds with real models and re-exports them to `seeds/runs/`.

### Deploying

The app container drives the host's Docker daemon (sandboxes are sibling containers), so it needs a VM
rather than a serverless container:

```bash
# on a Nebius Compute VM with Docker installed
git clone https://github.com/sja-thedude/bisect-agent && cd bisect-agent
cp .env.example .env && $EDITOR .env
sudo mkdir -p /var/lib/bisect
docker build -t bisect-sandbox:py3.11 sandbox/
docker compose up -d --build     # serves on port 80
```

`/var/lib/bisect` must be mounted at the same path inside and outside the container, because snapshots
are bind-mounted into sandbox containers by host path. Seeded runs in `seeds/runs/` load on startup.

## Repository layout

```
backend/bisect_agent/
  router.py     model routing table + call log (the only place model IDs live)
  nebius.py     Token Factory client (the only inference client)
  agent.py      the agent loop and its inspectable JobState
  sandbox.py    Docker sandbox: deps resolution, install, pytest runs
  gitops.py     bare clones, git bisect driver, speculative midpoint prediction
  tavily.py     Tavily search + dependency-diff parsing
  prompts.py    every prompt, one per routed task
  store.py      SQLite runs + events (permalinks, replay)
  server.py     FastAPI API + SSE + static UI
  cli.py        run / models / serve
sandbox/        sandbox image + in-container dependency resolver
web/            Next.js 15 + Tailwind v4 single-page UI (static export)
seeds/          seed catalog + exported demo runs
FRICTION_LOG.md notes on Nebius / NVIDIA / Tavily tooling, written as I built
```

## Scope

Python only, pytest only — by design. Pure-Python projects work best: the package is imported from the
checkout (repo root and `src/` on `PYTHONPATH`) rather than built, so projects that need compiled
extensions at test time are out of scope.

## Licence

[MIT](LICENSE)
