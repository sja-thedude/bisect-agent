"""The Bisect agent loop.

The job's entire state lives in one ``JobState`` dataclass that is serialised after every
change, so a run can be inspected (and replayed in the UI) at any point.

Phases:
  clone -> reproduce -> confirm -> bisect -> (research) -> explain -> fix -> verify -> done
Any phase can end the job early with partial findings when a hard limit is hit.
"""
from __future__ import annotations

import concurrent.futures as cf
import difflib
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import prompts
from .config import Settings
from .gitops import BisectSession, Repo, repo_slug, touches_dependencies, validate_repo_url, web_commit_url
from .nebius import InferenceError
from .router import CallRecord, Router, economics
from .sandbox import SUPPORTED_PYTHONS, Sandbox, SandboxError, TestRun
from .tavily import Tavily, parse_dependency_changes

REPRO_NAME = "test_bisect_repro.py"
# Failure messages that mean the test could not exercise the behaviour, rather than observing the bug.
_NOT_A_SYMPTOM = re.compile(r"\b(ImportError|ModuleNotFoundError|AttributeError: module|NameError|SyntaxError|"
                            r"fixture '.*' not found|cannot import name)\b")
MAX_REPRO_ATTEMPTS = 4


class LimitHit(Exception):
    pass


# ---------------------------------------------------------------------- state
@dataclass
class JobInput:
    repo_url: str
    bug_report: str
    good_ref: str | None = None
    bad_ref: str | None = None
    python: str = "3.11"
    propose_fix: bool = True


@dataclass
class Step:
    index: int
    sha: str
    subject: str
    status: str            # pytest status: passed | failed | error | timeout | ...
    verdict: str           # good | bad | skip
    reason: str
    duration_s: float
    tier: str
    model: str
    summary: str = ""
    speculative_hit: bool = False
    url: str = ""


@dataclass
class JobState:
    id: str
    input: JobInput
    phase: str = "queued"
    outcome: str = ""      # found | ambiguous | partial | failed
    error: str = ""
    repo_url: str = ""
    good_sha: str = ""
    bad_sha: str = ""
    range_size: int = 0
    repro: dict = field(default_factory=dict)
    head_run: dict = field(default_factory=dict)
    good_run: dict = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    first_bad: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    research: dict = field(default_factory=dict)
    explanation: dict = field(default_factory=dict)
    fix: dict = field(default_factory=dict)
    limits_hit: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    model_totals: dict = field(default_factory=dict)
    economics: dict = field(default_factory=dict)
    counterfactual: dict = field(default_factory=dict)
    range_commits: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


Emit = Callable[[str, dict], None]


def _sanitize(run: TestRun) -> dict:
    d = run.to_dict()
    d["output_tail"] = d["output_tail"][-4000:]
    return d


# ---------------------------------------------------------------------- agent
class Agent:
    def __init__(self, settings: Settings, state: JobState, emit: Emit, router: Router | None = None,
                 tavily: Tavily | None = None):
        self.s = settings
        self.state = state
        self._emit = emit
        self.router = router or Router()
        self.tavily = tavily or Tavily()
        self.deadline = time.monotonic() + settings.limits.job_timeout_s
        self.router.on_call(self._on_call)
        py = state.input.python if state.input.python in SUPPORTED_PYTHONS else "3.11"
        self.sandbox = Sandbox(settings, python=py)
        self.repo: Repo | None = None
        self._reverted: tuple[Path, str] | None = None  # HEAD with the culprit reverted, if that passed
        self.snap_key = ""
        self._deps_cache: dict[str, str] = {}
        self._pool = cf.ThreadPoolExecutor(max_workers=max(1, settings.limits.parallel_runs))
        self._llm_pool = cf.ThreadPoolExecutor(max_workers=4)

    # ------------------------------------------------------------- plumbing
    def emit(self, kind: str, **payload: Any) -> None:
        self._emit(kind, payload)

    def log(self, msg: str, level: str = "info") -> None:
        self.emit("log", message=msg, level=level)

    def phase(self, name: str) -> None:
        self.state.phase = name
        self.emit("phase", phase=name)

    def _on_call(self, rec: CallRecord) -> None:
        self.state.calls.append(rec.to_dict())
        self.state.model_totals = self.router.totals()
        self.state.economics = economics(self.state.calls)
        self.emit("call", **rec.to_dict())

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check_time(self, need: float = 0) -> None:
        if self.remaining() <= need:
            raise LimitHit(f"job time limit of {self.s.limits.job_timeout_s // 60} minutes reached")

    def test_timeout(self) -> int:
        return int(max(10, min(self.s.limits.test_timeout_s, self.remaining() - 5)))

    # ---------------------------------------------------------- entrypoint
    def run(self) -> JobState:
        st = self.state
        try:
            self.sandbox.ensure_ready()
            self._clone()
            self._reproduce()
            self._bisect()
            if st.first_bad:
                self._research_and_explain()
                if st.input.propose_fix and st.outcome == "found":
                    self._fix()
        except LimitHit as e:
            st.limits_hit.append(str(e))
            st.outcome = st.outcome or "partial"
            self.log(f"Limit reached: {e}. Reporting partial findings.", "warn")
        except (SandboxError, InferenceError) as e:
            st.outcome = st.outcome or "failed"
            st.error = str(e)
            self.log(f"Stopped: {e}", "error")
        except Exception as e:  # noqa: BLE001 - surface anything to the UI rather than dying silently
            st.outcome = st.outcome or "failed"
            st.error = f"{type(e).__name__}: {e}"
            self.log(traceback.format_exc()[-1500:], "error")
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._llm_pool.shutdown(wait=False, cancel_futures=True)
            if self.repo and self.repo.path.exists():
                shutil.rmtree(self.repo.path, ignore_errors=True)
            st.finished_at = time.time()
            if st.outcome in ("", "running"):
                st.outcome = "found" if st.first_bad else "partial"
            self.phase("done")
            self.emit("report", state=st.to_dict())
        return st

    # --------------------------------------------------------------- clone
    def _clone(self) -> None:
        st = self.state
        self.phase("clone")
        url = validate_repo_url(st.input.repo_url)
        st.repo_url = url.removesuffix(".git")
        self.log(f"Cloning {st.repo_url}")
        with _repo_lock(url):
            cache = Repo.clone(url, self.s.repos_dir)
        # Each job gets its own bare clone sharing objects with the cache, so concurrent
        # jobs on the same repo never fight over git bisect state.
        job_dir = self.s.data_dir / "jobs" / st.id / "repo.git"
        shutil.rmtree(job_dir.parent, ignore_errors=True)  # left behind if a previous run with this id was killed
        job_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--bare", "--shared", str(cache.path), str(job_dir)],
                       check=True, capture_output=True)
        self.repo = Repo(job_dir, url)
        self.snap_key = repo_slug(url)
        bad_ref = st.input.bad_ref or "HEAD"
        st.bad_sha = self._resolve(cache, bad_ref)
        if not st.bad_sha:
            raise ValueError(f"unknown bad ref {bad_ref!r}")
        if st.input.good_ref:
            st.good_sha = self._resolve(cache, st.input.good_ref)
            if not st.good_sha:
                raise ValueError(f"unknown good ref {st.input.good_ref!r}")
            if not self.repo.is_ancestor(st.good_sha, st.bad_sha):
                raise ValueError("the known-good ref must be an ancestor of the bad ref")
        else:
            st.good_sha = self.repo.default_good(st.bad_sha, self.s.limits.default_range)
            self.log(f"No known-good ref given; defaulting to {self.s.limits.default_range} commits back.")
        st.range_size = self.repo.count_between(st.good_sha, st.bad_sha)
        self.log(f"Range: {st.good_sha[:10]}..{st.bad_sha[:10]} ({st.range_size} commits)")

    def _resolve(self, cache: Repo, ref: str) -> str:
        """Resolve a ref; refs that are not branches or tags (e.g. pull/598/head, an unreachable
        sha) are fetched on demand into the cache and the job clone."""
        sha = self.repo.rev(ref)
        if sha:
            return sha
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref) or ref.startswith("-"):
            return ""
        local = f"refs/bisect-fetched/{re.sub(r'[^A-Za-z0-9._-]', '_', ref)}"
        with _repo_lock(cache.url):
            cache.git("fetch", "--quiet", "origin", f"+{ref}:{local}", check=False, timeout=300)
        self.repo.git("fetch", "--quiet", str(cache.path), f"+{local}:{local}", check=False, timeout=300)
        return self.repo.rev(local)

    # ------------------------------------------------------------ sandbox
    def _deps_key(self, snap: Path) -> tuple[str, bool]:
        reqs, cons = self.sandbox.requirements_for(snap)
        cache_key = "\n".join(sorted(reqs)) + "|" + "\n".join(sorted(cons))
        if cache_key in self._deps_cache:
            return self._deps_cache[cache_key], True
        key, ok, log = self.sandbox.install(reqs, cons)
        if ok:
            self._deps_cache[cache_key] = key
        else:
            self.log(f"Dependency install failed for {len(reqs)} requirements: {log[-300:]}", "warn")
        return key, ok

    def _run_repro(self, sha: str, code: str | None = None) -> TestRun:
        snap = self.sandbox.snapshot(self.repo.path, sha, self.snap_key)
        key, ok = self._deps_key(snap)
        if not ok:
            return TestRun(sha=sha, status="install_failed", exit_code=None, duration_s=0.0,
                           output_tail="dependency installation failed", deps_key=key)
        path = self.state.repro["path"]
        return self.sandbox.run_pytest(snap, sha, key, inject={path: code or self.state.repro["code"]},
                                       targets=[path], timeout_s=self.test_timeout())

    # ---------------------------------------------------------- reproduce
    def _gather_context(self) -> str:
        st = self.state
        tree = [p for p in self.repo.tree(st.bad_sha) if p.endswith((".py", ".toml", ".cfg", ".txt", ".md", ".rst"))]
        listing = "\n".join(tree[:1500])
        sel = self.router.call(
            "select_context_files", prompts.SELECT_CONTEXT_FILES,
            f"BUG REPORT:\n{st.input.bug_report}\n\nFILES:\n{listing}", json_out=True,
        ).data or {}
        files = [f for f in (sel.get("source_files") or [])[:6] + (sel.get("test_files") or [])[:2] if f in tree]
        for must in ("pyproject.toml", "setup.py", "setup.cfg"):
            if must in tree and must not in files:
                files.append(must)
        parts = [f"PACKAGE IMPORT HINT: {sel.get('package_import', '')}"]
        for f in files:
            body = self.repo.file_at(st.bad_sha, f, 24000) or ""
            parts.append(f"===== {f} (at HEAD) =====\n{body}")
        st.repro["context_files"] = files
        self.log(f"Reading {len(files)} files for context: {', '.join(files)}")
        return "\n\n".join(parts)

    def _repro_path(self) -> str:
        tree = set(self.repo.tree(self.state.bad_sha))
        for d in ("tests", "test", "testing"):
            if any(p.startswith(d + "/") for p in tree):
                return f"{d}/{REPRO_NAME}"
        return REPRO_NAME

    def _reproduce(self) -> None:
        """Ultra writes several candidate tests at once; each attempt runs them at HEAD and at the
        known-good commit in parallel, and the first candidate that fails at HEAD (with a real
        assertion) and passes at known-good is kept. Failing attempts go back to Ultra with a
        per-candidate table and the full history, so repairs converge instead of oscillating."""
        st = self.state
        self.phase("reproduce")
        context = self._gather_context()
        st.repro["path"] = self._repro_path()
        user = (f"BUG REPORT:\n{st.input.bug_report}\n\nThe test file will be written to `{st.repro['path']}`.\n\n"
                f"REPOSITORY CONTEXT:\n{context}")
        res = self.router.call("write_repro_test", prompts.WRITE_REPRO_TEST, user, json_out=True)
        self._set_repro(res.data, attempt=1)
        history: list[str] = []

        for attempt in range(1, MAX_REPRO_ATTEMPTS + 1):
            self.check_time(60)
            self.phase("confirm")
            self.log(f"Running candidate tests at HEAD {st.bad_sha[:10]} and known-good {st.good_sha[:10]} "
                     f"in parallel (attempt {attempt})")
            f_head = self._pool.submit(self._run_repro, st.bad_sha)
            f_good = self._pool.submit(self._run_repro, st.good_sha)
            head, good = f_head.result(), f_good.result()
            table, winners = _candidate_table(head, good)
            if winners:
                code = _keep_tests(st.repro["code"], winners)
                if code != st.repro["code"]:
                    self.log(f"Kept {len(winners)} of {len(table)} candidates: {', '.join(winners)}.")
                    st.repro["code"] = code
                    self.emit("repro", path=st.repro["path"], code=code, rationale=st.repro.get("rationale", ""),
                              symptom=st.repro.get("symptom", ""), attempt=attempt, kept=winners)
                    head = self._run_repro(st.bad_sha)  # clean reference failure for the classifier
                st.head_run, st.good_run = _sanitize(head), _sanitize(good)
                self.log(f"Reproduction confirmed: fails at HEAD ({head.duration_s}s), passes at known-good.")
                self.emit("repro_confirmed", head=st.head_run, good=st.good_run)
                return
            st.head_run, st.good_run = _sanitize(head), _sanitize(good)
            summary = "; ".join(f"{name}: HEAD {h}, known-good {g}" for name, h, g, _ in table) or \
                f"no test results (HEAD pytest {head.status}, known-good pytest {good.status})"
            history.append(f"Attempt {attempt}: {summary}")
            if attempt == MAX_REPRO_ATTEMPTS:
                break
            self.log(f"No candidate reproduces cleanly ({summary}). Asking for revised candidates.", "warn")
            self.phase("reproduce")
            rows = "\n".join(f"- {name}: at HEAD {h}{(' - ' + msg) if msg else ''}; at known-good {g}"
                             for name, h, g, msg in table)
            repair_user = (
                f"BUG REPORT:\n{st.input.bug_report}\n\nCURRENT CANDIDATES ({st.repro['path']}):\n"
                f"```python\n{st.repro['code']}\n```\n\nRESULTS (required: FAIL with an assertion at HEAD, PASS at "
                f"known-good):\n{rows or '(no per-test results; see output)'}\n\nHISTORY OF EARLIER ATTEMPTS:\n"
                + "\n".join(history) +
                f"\n\nPYTEST OUTPUT AT HEAD:\n{head.output_tail[-3000:]}\n\nPYTEST OUTPUT AT KNOWN-GOOD:\n"
                f"{good.output_tail[-3000:]}\n\nREPOSITORY CONTEXT:\n{context[:50000]}{self._good_ref_context()}")
            res = self.router.call("repair_repro_test", prompts.REPAIR_REPRO_TEST, repair_user, json_out=True)
            self._set_repro(res.data, attempt=attempt + 1)
        st.outcome = "failed"
        raise LimitHit(f"could not produce a test that fails at HEAD and passes at the known-good commit "
                       f"after {MAX_REPRO_ATTEMPTS} attempts")

    def _good_ref_context(self) -> str:
        st = self.state
        out = ["\n\nTHE SAME FILES AT THE KNOWN-GOOD COMMIT (use only APIs that exist here):"]
        for f in st.repro.get("context_files", [])[:6]:
            body = self.repo.file_at(st.good_sha, f, 15000)
            if body:
                out.append(f"===== {f} (at known-good) =====\n{body}")
        return "\n".join(out)

    def _set_repro(self, data: dict, attempt: int) -> None:
        code = (data or {}).get("test_code", "")
        if "def test" not in code:
            raise InferenceError("model did not return a pytest test")
        self.state.repro.update(code=code, rationale=data.get("rationale", ""), symptom=data.get("symptom", ""),
                                attempts=attempt)
        self.emit("repro", path=self.state.repro["path"], code=code, rationale=data.get("rationale", ""),
                  symptom=data.get("symptom", ""), attempt=attempt)

    # -------------------------------------------------------------- bisect
    def _classify(self, run: TestRun) -> tuple[str, str, CallRecord | None]:
        """Cheap tier reads the pytest output; hard evidence overrides it when they disagree."""
        st = self.state
        per_test = ", ".join(f"{_case_name(c.nodeid)}={c.outcome}" for c in run.cases) or "none collected"
        user = (f"RESULT AT THIS COMMIT ({run.sha[:10]}): pytest status={run.status.upper()} (exit {run.exit_code}); "
                f"tests: {per_test}\n\nOUTPUT AT THIS COMMIT:\n{run.output_tail[-3000:]}\n\n"
                f"BUG SYMPTOM: {st.repro.get('symptom', '')}\n\n"
                f"FOR COMPARISON ONLY - the failure seen at HEAD, a different commit:\n"
                f"{st.head_run.get('output_tail', '')[-2000:]}")
        verdict, reason, rec = None, "", None
        try:
            res = self.router.call("classify_verdict", prompts.CLASSIFY_VERDICT, user, json_out=True, context=run.sha)
            verdict = (res.data or {}).get("verdict")
            reason = (res.data or {}).get("reason", "")
            rec = res.record
        except InferenceError as e:
            reason = f"classifier unavailable ({e}); used exit code"
        hard = {"passed": "good", "timeout": "skip", "install_failed": "skip", "no_tests": "skip",
                "error": "skip"}.get(run.status)
        failed = [c for c in run.cases if c.outcome == "failed"]
        if run.status == "failed" and failed and all(_NOT_A_SYMPTOM.search(c.message) for c in failed):
            # The test could not even exercise the behaviour here (API missing etc.): never call that "bad".
            hard = "skip"
        if hard and verdict != hard:
            reason = f"{reason} [overridden: pytest status {run.status} -> {hard}]".strip()
            verdict = hard
        if verdict not in ("good", "bad", "skip"):
            verdict = "bad" if run.status == "failed" else "skip"
        return verdict, reason, rec

    def _summarise(self, sha: str) -> str:
        try:
            diff = self.repo.diff(sha, max_chars=12000)
            res = self.router.call("summarise_diff", prompts.SUMMARISE_DIFF,
                                   f"BUG REPORT: {self.state.input.bug_report[:600]}\n\nDIFF:\n{diff}",
                                   json_out=True, context=sha)
            return (res.data or {}).get("summary", "")
        except InferenceError:
            return ""

    def _bisect(self) -> None:
        st = self.state
        self.phase("bisect")
        session = BisectSession(self.repo)
        status = session.start(st.bad_sha, st.good_sha)
        shas = self.repo.git("rev-list", "--reverse", "--topo-order", st.bad_sha, f"^{st.good_sha}", "--",
                             *session.pathspec).split()
        st.range_commits = [{"sha": c} for c in shas[-400:]]
        self.emit("range", good=st.good_sha, bad=st.bad_sha, commits=[c["sha"] for c in st.range_commits],
                  filtered_out=st.range_size - len(shas))
        if st.range_size - len(shas) > 0:
            self.log(f"Pathspec filter: {st.range_size - len(shas)} docs/CI-only commits excluded from the bisect.")
        # Seed the cache with the two runs we already have.
        results: dict[str, cf.Future] = {}
        for sha, d in ((st.bad_sha, st.head_run), (st.good_sha, st.good_run)):
            f: cf.Future = cf.Future()
            f.set_result(TestRun(**{**d, "cases": []}))
            results[sha] = f

        def launch(sha: str) -> None:
            if sha not in results and self.remaining() > 30:
                results[sha] = self._pool.submit(self._run_repro, sha)

        try:
            while not status.done:
                if len(st.steps) >= self.s.limits.max_bisect_steps:
                    raise LimitHit(f"maximum of {self.s.limits.max_bisect_steps} bisect steps reached")
                self.check_time(10)
                sha = status.next_sha
                was_prefetched = sha in results
                launch(sha)
                # Speculatively start the commits git will ask about next, whichever way this one goes.
                for nxt in session.predict(sha)[: max(0, self.s.limits.parallel_runs - 1)]:
                    launch(nxt)
                summary_f = self._llm_pool.submit(self._summarise, sha)
                commit = self.repo.commit(sha)
                self.emit("step_start", index=len(st.steps) + 1, sha=sha, subject=commit.subject,
                          url=web_commit_url(st.repo_url, sha), remaining=session.remaining())
                try:
                    run = results[sha].result(timeout=max(1, self.remaining()))
                except cf.TimeoutError:
                    raise LimitHit("job time limit reached while a test was running")
                verdict, reason, rec = self._classify(run)
                step = Step(index=len(st.steps) + 1, sha=sha, subject=commit.subject, status=run.status,
                            verdict=verdict, reason=reason, duration_s=run.duration_s,
                            tier=rec.tier if rec else "", model=rec.model if rec else "",
                            summary=summary_f.result(), speculative_hit=was_prefetched,
                            url=web_commit_url(st.repo_url, sha))
                st.steps.append(step)
                self.emit("step", **asdict(step))
                status = session.mark(sha, verdict)
        except LimitHit:
            self._record_partial_range(session)
            raise
        finally:
            for f in results.values():
                f.cancel()  # speculative runs git never asked for
            bisect_log = session.log()
            session.reset()
            st.repro["bisect_log"] = bisect_log

        if status.first_bad:
            self._set_first_bad(status.first_bad)
            st.outcome = "found"
        elif status.candidates:
            self._resolve_candidates(status.candidates)

    def _good_refs(self) -> list[str]:
        refs = self.repo.git("for-each-ref", "--format=%(refname)", "refs/bisect/", check=False).split()
        return [r for r in refs if r.startswith("refs/bisect/good-")]

    def _record_partial_range(self, session: BisectSession) -> None:
        st = self.state
        try:
            bad = self.repo.git("rev-parse", "refs/bisect/bad", check=False).strip()
            goods = self._good_refs()
            shas = self.repo.git("rev-list", bad, *[f"^{g}" for g in goods], check=False).split()
        except Exception:  # noqa: BLE001
            return
        st.candidates = [{"sha": s, "subject": self.repo.commit(s).subject, "url": web_commit_url(st.repo_url, s)}
                         for s in shas[:50]]
        self.log(f"Narrowed the regression to {len(shas)} commits before stopping.", "warn")

    def _set_first_bad(self, sha: str) -> None:
        st = self.state
        c = self.repo.commit(sha)
        files = self.repo.changed_files(sha)
        st.first_bad = {"sha": sha, "subject": c.subject, "author": c.author, "date": c.date,
                        "url": web_commit_url(st.repo_url, sha), "files": files,
                        "dependency_files": touches_dependencies(files)}
        self.log(f"First bad commit: {sha[:10]} {c.subject}")
        self.emit("first_bad", **st.first_bad)

    def _resolve_candidates(self, shas: list[str]) -> None:
        st = self.state
        cands = []
        for s in shas:
            c = self.repo.commit(s)
            cands.append({"sha": s, "subject": c.subject, "url": web_commit_url(st.repo_url, s),
                          "files": self.repo.changed_files(s)[:20]})
        st.candidates = cands
        self.log(f"Untestable commits left {len(shas)} candidates; ranking them with the fast tier.", "warn")
        user = f"BUG REPORT:\n{st.input.bug_report}\n\nCANDIDATES:\n" + "\n\n".join(
            f"{c['sha']} {c['subject']}\nfiles: {', '.join(c['files'])}\n{self.repo.diff(c['sha'], 3000)}" for c in cands)
        try:
            res = self.router.call("filter_candidates", prompts.FILTER_CANDIDATES, user, json_out=True)
            ranked = [s for s in (res.data or {}).get("ranked", []) if s in shas]
            st.candidates.sort(key=lambda c: ranked.index(c["sha"]) if c["sha"] in ranked else 99)
            if ranked:
                self._set_first_bad(ranked[0])
                st.first_bad["ambiguous"] = True
                st.first_bad["ranking_reason"] = (res.data or {}).get("reason", "")
        except InferenceError:
            pass
        st.outcome = "ambiguous"

    # --------------------------------------------------- research + explain
    def _research_and_explain(self) -> None:
        st = self.state
        self.phase("explain")
        sha = st.first_bad["sha"]
        diff = self.repo.diff(sha, max_chars=50000)
        dep_changes = parse_dependency_changes(diff) if st.first_bad.get("dependency_files") else []
        st.first_bad["dependency_changes"] = dep_changes

        cf_pool = cf.ThreadPoolExecutor(max_workers=1)
        counterfactual_f = cf_pool.submit(self._counterfactual)
        cf_pool.shutdown(wait=False)
        findings_text = ""
        if st.first_bad.get("dependency_files"):
            # The agent decides for itself whether the repo's history is enough to explain the break.
            decision = self.router.call(
                "decide_upstream_research", prompts.DECIDE_UPSTREAM_RESEARCH,
                f"BUG REPORT:\n{st.input.bug_report}\n\nDEPENDENCY CHANGES PARSED FROM THE DIFF: {dep_changes}\n\n"
                f"FAILURE AT FIRST BAD COMMIT:\n{st.head_run.get('output_tail', '')[-2000:]}\n\nDIFF:\n{diff[:30000]}",
                json_out=True, context=sha,
            ).data or {}
            st.research["decision"] = decision
            self.emit("research_decision", **decision)
            if decision.get("needs_upstream_research"):
                findings_text = self._upstream_research(decision, dep_changes)
            else:
                self.log("Dependency files changed, but the agent judged the source diff sufficient to explain the break.")
        else:
            st.research["decision"] = {"needs_upstream_research": False,
                                       "reason": "The first bad commit does not touch any dependency files."}

        try:
            st.counterfactual = counterfactual_f.result(timeout=max(1, self.remaining()))
        except Exception as e:  # noqa: BLE001 - the proof is extra evidence, never fatal
            st.counterfactual = {"status": "error", "detail": str(e)[:300]}
        self.emit("counterfactual", **st.counterfactual)
        if st.phase != "explain":
            self.phase("explain")
        cf_text = {
            "confirmed": "COUNTERFACTUAL CHECK: reverting ONLY this commit on top of HEAD makes the reproduction test "
                         "pass. The commit is causally responsible, not merely the first failing point.",
            "not_confirmed": "COUNTERFACTUAL CHECK: reverting this commit on HEAD does NOT make the test pass, so later "
                             "commits also contribute. Say so in the explanation.",
        }.get(st.counterfactual.get("status"), "")
        parent = st.first_bad["sha"] + "^"
        self.log("Explaining the culprit commit with the reasoning tier.")
        culprit_run = next((s for s in st.steps if s.sha == sha), None)
        user = (f"BUG REPORT:\n{st.input.bug_report}\n\nREPRODUCTION TEST:\n```python\n{st.repro['code']}\n```\n\n"
                f"FIRST BAD COMMIT: {sha} {st.first_bad['subject']}\nPytest at first bad commit: "
                f"{culprit_run.status if culprit_run else 'failed'}\nReference failure output:\n"
                f"{st.head_run.get('output_tail', '')[-3000:]}\n\nPARENT {parent}: test passed.\n\n"
                f"{cf_text}\n\n{findings_text}\n\nDIFF OF FIRST BAD COMMIT:\n{diff}")
        res = self.router.call("explain_culprit", prompts.EXPLAIN_CULPRIT, user, json_out=True, context=sha)
        st.explanation = res.data or {}
        self.emit("explanation", **st.explanation)

    def _counterfactual(self) -> dict:
        """Causal proof: revert ONLY the culprit on top of HEAD and rerun the reproduction test.
        Bisect shows where the test starts failing; this shows that the commit is why it fails today."""
        st = self.state
        sha = st.first_bad["sha"]
        self.log(f"Counterfactual: reverting {sha[:10]} alone on top of HEAD and re-running the test.")
        tree, err = self.repo.revert_tree(st.bad_sha, sha)
        skipped: list[str] = []
        if tree:
            patched = self.sandbox.snapshot(self.repo.path, tree, self.snap_key + "-reverts")
            revert_diff = self.repo.git("diff", st.bad_sha, tree, check=False)
        else:
            conflicted = [f.strip() for f in err.removeprefix("conflicts in:").split(",") if f.strip()]
            if any(_is_code(f) for f in conflicted) or not err.startswith("conflicts in:"):
                return self._resolved_counterfactual(sha, conflicted, err)
            # Only non-code files conflict (version files, changelogs): revert the culprit's
            # changes to every other file and say which were left alone.
            skipped = conflicted
            keep = [f for f in self.repo.changed_files(sha) if f not in skipped]
            reverse = self.repo.git("diff", "--binary", sha, f"{sha}^", "--", *keep) if keep else ""
            patched, apply_err = self._apply_patch(reverse, "reverted") if reverse else (None, "nothing left to revert")
            if not patched:
                self.log("Counterfactual: the culprit no longer reverts on HEAD (later commits changed the same lines).")
                return {"status": "conflict", "detail": (err + " " + apply_err)[:400]}
            self.log(f"Counterfactual: full revert conflicts in {', '.join(skipped)}; reverting the other "
                     f"{len(keep)} file(s) of the culprit.")
            revert_diff = reverse
        key, ok = self._deps_key(patched)
        if not ok:
            return {"status": "install_failed", "detail": "dependencies for the reverted tree failed to install"}
        run = self.sandbox.run_pytest(patched, st.bad_sha, key, inject={st.repro["path"]: st.repro["code"]},
                                      targets=[st.repro["path"]], timeout_s=self.test_timeout())
        status = _counterfactual_status(run.status)
        if status == "confirmed":
            self._reverted = (patched, revert_diff)
        self.log(f"Counterfactual {status.upper().replace('_', ' ')}: "
                 f"HEAD minus {sha[:10]} -> pytest {run.status} ({run.duration_s}s).",
                 "info" if status == "confirmed" else "warn")
        return {"status": status, "pytest": run.status, "duration_s": run.duration_s, "partial": bool(skipped),
                "skipped_files": skipped, "method": "git_revert", "revert_diff": revert_diff[:20000],
                "output_tail": run.output_tail[-1500:]}

    def _resolved_counterfactual(self, sha: str, conflicted: list[str], err: str) -> dict:
        """Later commits rewrote the culprit's lines, so `git revert` conflicts. The reasoning tier
        writes the revert against today's code instead; the sandbox, not the model, decides the result."""
        st = self.state
        self.log(f"Counterfactual: `git revert {sha[:10]}` conflicts with later edits to {', '.join(conflicted) or 'the same lines'}; "
                 "asking the reasoning tier to resolve the revert against HEAD.")
        files = [f for f in self.repo.changed_files(sha)
                 if f.endswith((".py", ".pyx", ".pyi", ".txt", ".toml", ".cfg", ".lock")) and self.repo.file_at(st.bad_sha, f)][:8]
        head_files = "\n\n".join(f"===== {f} (at HEAD) =====\n{self.repo.file_at(st.bad_sha, f, 30000)}" for f in files)
        user = (f"COMMIT TO REVERT: {sha} {st.first_bad.get('subject', '')}\n\nITS DIFF:\n{self.repo.diff(sha, 30000)}\n\n"
                f"HEAD FILES:\n{head_files}")
        feedback = ""
        for attempt in range(1, 3):
            try:
                data = self.router.call("resolve_revert", prompts.RESOLVE_REVERT, user + feedback, json_out=True,
                                        context=sha).data or {}
            except InferenceError as e:
                return {"status": "conflict", "detail": f"{err} ({e})"[:400]}
            patched, apply_err, patch = self._apply_edits(data.get("edits") or [], f"reverted-{attempt}")
            if patched:
                break
            feedback = f"\n\nYOUR EDITS COULD NOT BE APPLIED:\n{apply_err}\nCopy `find` snippets verbatim from HEAD FILES."
        else:
            return {"status": "conflict", "detail": err[:400]}
        key, ok = self._deps_key(patched)
        if not ok:
            return {"status": "install_failed", "detail": "dependencies for the reverted tree failed to install"}
        run = self.sandbox.run_pytest(patched, st.bad_sha, key, inject={st.repro["path"]: st.repro["code"]},
                                      targets=[st.repro["path"]], timeout_s=self.test_timeout())
        status = _counterfactual_status(run.status)
        if status == "confirmed":
            self._reverted = (patched, patch)
        self.log(f"Counterfactual {status.upper().replace('_', ' ')} (revert resolved by the "
                 f"reasoning tier): HEAD minus {sha[:10]} -> pytest {run.status} ({run.duration_s}s).",
                 "info" if status == "confirmed" else "warn")
        return {"status": status, "pytest": run.status, "duration_s": run.duration_s, "method": "model_resolved",
                "conflicted_files": conflicted, "revert_diff": patch[:20000], "output_tail": run.output_tail[-1500:]}

    def _upstream_research(self, decision: dict, dep_changes: list[dict]) -> str:
        st = self.state
        self.phase("research")
        if not self.tavily.configured:
            self.log("Upstream research warranted, but TAVILY_API_KEY is not set; skipping.", "warn")
            st.research["skipped"] = "TAVILY_API_KEY not set"
            return ""
        packages = decision.get("packages") or [
            {"name": c["package"], "old_version": c["old"], "new_version": c["new"]} for c in dep_changes]
        st.research["packages"] = packages
        searches: list[dict] = []
        for pkg in packages[:3]:
            plan = self.router.call(
                "plan_search_queries", prompts.PLAN_SEARCH_QUERIES,
                f"PACKAGE: {pkg}\nSYMPTOM IN DOWNSTREAM PROJECT: {st.repro.get('symptom') or st.input.bug_report[:500]}",
                json_out=True,
            ).data or {}
            for q in (plan.get("queries") or [])[:5]:
                domains = {"cve": ["nvd.nist.gov", "github.com", "osv.dev", "cve.org"],
                           "issues": ["github.com"]}.get(q.get("purpose", ""))
                rec = self.tavily.search(q["query"], q.get("purpose", "other"), include_domains=domains)
                self.emit("search", package=pkg.get("name"), **rec.to_dict())
                searches.append({"package": pkg.get("name"), **rec.to_dict()})
        st.research["searches"] = searches
        hits_text = "\n\n".join(
            f"[{s['purpose']}] {s['query']}\nAnswer: {s['answer']}\n" + "\n".join(
                f"- {h['title']} ({h['url']}): {h['content'][:700]}" for h in s["hits"][:4])
            for s in searches if not s["error"])
        if not hits_text:
            return ""
        digest = self.router.call(
            "digest_search_results", prompts.DIGEST_SEARCH_RESULTS,
            f"PACKAGES: {packages}\nDOWNSTREAM SYMPTOM: {st.repro.get('symptom', '')}\n\nRESULTS:\n{hits_text[:40000]}",
            json_out=True,
        ).data or {}
        st.research["findings"] = digest.get("findings", [])
        st.research["upstream_change"] = digest.get("upstream_change", "")
        self.emit("research", findings=st.research["findings"], upstream_change=st.research["upstream_change"])
        self.phase("explain")
        lines = "\n".join(f"- {f.get('fact')} [{f.get('url')}]" for f in st.research["findings"])
        return f"UPSTREAM RESEARCH FINDINGS (via web search):\n{st.research['upstream_change']}\n{lines}"

    # ------------------------------------------------------------ fix + verify
    def _fix(self) -> None:
        """Self-correcting fix loop: Ultra proposes edits, the sandbox runs the reproduction test on the
        patched HEAD, and failures are fed back until it passes (max 3 attempts). Only a fix that
        passes the reproduction test goes on to the full-suite verification."""
        st = self.state
        self.check_time(120)
        self.phase("fix")
        sha = st.first_bad["sha"]
        candidates = st.first_bad.get("files", []) + [f for f in st.repro.get("context_files", [])
                                                         if _is_code(f) and "test" not in f.rsplit("/", 1)[-1]]
        files, budget = [], 120_000
        for f in dict.fromkeys(candidates):
            body = self.repo.file_at(st.bad_sha, f, 30000) if (_is_code(f) or f in st.first_bad.get("files", [])) else None
            if body is not None and budget - len(body) > 0 and len(files) < 10:
                files.append((f, body))
                budget -= len(body)
        head_files = "\n\n".join(f"===== {f} (at HEAD) =====\n{body}" for f, body in files)
        base_user = (f"BUG REPORT:\n{st.input.bug_report}\n\nEXPLANATION:\n{st.explanation}\n\n"
                     f"REPRODUCTION TEST ({st.repro['path']}):\n```python\n{st.repro['code']}\n```\n\n"
                     f"CULPRIT DIFF:\n{self.repo.diff(sha, 30000)}\n\nHEAD FILES:\n{head_files}")
        feedback = ""
        attempts: list[dict] = []
        for attempt in range(1, 4):
            self.check_time(60)
            res = self.router.call("propose_fix", prompts.PROPOSE_FIX, base_user + feedback, json_out=True)
            data = res.data or {}
            patched, err, patch = self._apply_edits(data.get("edits") or [], f"patched-{attempt}")
            st.fix = {"diff": patch, "rationale": data.get("rationale", ""), "attempt": attempt, "attempts": attempts}
            if not patched:
                self.log(f"Fix attempt {attempt}: edits did not apply ({err[:160]}).", "warn")
                attempts.append({"attempt": attempt, "result": "did not apply", "detail": err[:300]})
                feedback = (f"\n\nYOUR PREVIOUS EDITS COULD NOT BE APPLIED:\n{err}\nCopy `find` snippets verbatim from the "
                            "HEAD FILES section (not from the culprit diff: the code may have been reformatted since).")
                continue
            self.emit("fix", diff=patch, rationale=st.fix["rationale"], attempt=attempt)
            key, ok = self._deps_key(patched)
            run = self.sandbox.run_pytest(patched, st.bad_sha, key, inject={st.repro["path"]: st.repro["code"]},
                                          targets=[st.repro["path"]], timeout_s=self.test_timeout()) if ok else None
            if run and run.status == "passed":
                attempts.append({"attempt": attempt, "result": "reproduction passes"})
                self.log(f"Fix attempt {attempt}: reproduction test passes on HEAD + fix.")
                st.fix["applied"] = True
                self._verify(patched)
                return
            status = run.status if run else "install_failed"
            attempts.append({"attempt": attempt, "result": f"reproduction {status}"})
            self.log(f"Fix attempt {attempt}: reproduction test still {status}; feeding the failure back.", "warn")
            feedback = (f"\n\nYOUR PREVIOUS FIX (attempt {attempt}):\n{patch}\n\nThe reproduction test still fails on "
                        f"HEAD + that fix:\n{(run.output_tail if run else '')[-3000:]}\nWrite a corrected fix against "
                        f"the ORIGINAL HEAD files (not on top of your previous attempt).")
        if self._reverted:
            patched, diff = self._reverted
            self.log("No targeted fix passed within 3 attempts; falling back to the revert that the causal check "
                     "already proved restores the behaviour, and verifying it against the full suite.", "warn")
            attempts.append({"attempt": len(attempts) + 1, "result": "fallback: revert of the culprit"})
            st.fix = {"diff": diff, "attempt": len(attempts), "attempts": attempts, "fallback": "revert",
                      "rationale": (f"Fallback: revert {sha[:10]} on HEAD. A targeted fix that keeps the commit's other "
                                    "changes did not pass within 3 attempts; this restores the old behaviour and is "
                                    "checked against the full suite like any other fix."), "applied": True}
            self.emit("fix", diff=diff, rationale=st.fix["rationale"], attempt=len(attempts))
            self._verify(patched)
            return
        st.fix["verdict"] = "No working fix found"
        self.log("No fix made the reproduction test pass within 3 attempts.", "warn")

    def _apply_edits(self, edits: list, name: str) -> tuple[Path | None, str, str]:
        """Apply search/replace edits to a copy of the HEAD snapshot. Returns (dir, error, unified diff).
        Pure file manipulation on the host; nothing from the repository executes."""
        st = self.state
        if not edits:
            return None, "no edits", ""
        base = self.sandbox.snapshot(self.repo.path, st.bad_sha, self.snap_key)
        dest = self.s.data_dir / "jobs" / st.id / name
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(base, dest, symlinks=True)
        originals: dict[str, str] = {}
        for e in edits:
            rel = os.path.normpath(str(e.get("path", ""))).lstrip("/")
            target = (dest / rel).resolve()
            if rel.startswith("..") or not target.is_relative_to(dest.resolve()) or not target.is_file():
                return None, f"{rel}: not an existing file in the repository", ""
            if rel == os.path.normpath(st.repro["path"]):
                return None, "edits must not touch the reproduction test", ""
            text = target.read_text(errors="replace")
            originals.setdefault(rel, text)
            find, replace = str(e.get("find", "")), str(e.get("replace", ""))
            new = _replace_once(text, find, replace)
            if new is None:
                near = _closest_region(text, find)
                hint = f"\nThe closest text actually in the HEAD file is:\n{near}" if near else ""
                return None, f"{rel}: `find` snippet does not occur exactly once:\n{find[:400]}{hint}", ""
            target.write_text(new)
        diff = "".join(
            "".join(difflib.unified_diff(old.splitlines(True), (dest / rel).read_text().splitlines(True),
                                         f"a/{rel}", f"b/{rel}"))
            for rel, old in originals.items())
        if not diff.strip():
            return None, "edits made no change", ""
        return dest, "", diff

    def _apply_patch(self, patch: str, name: str = "patched") -> tuple[Path | None, str]:
        st = self.state
        if not patch.strip():
            return None, "empty diff"
        base = self.sandbox.snapshot(self.repo.path, st.bad_sha, self.snap_key)
        dest = self.s.data_dir / "jobs" / st.id / name
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(base, dest, symlinks=True)
        # Applying a patch is pure file manipulation on the host; nothing from the repo executes.
        proc = subprocess.run(["git", "apply", "--recount", "--whitespace=nowarn", "-"], input=patch, text=True,
                              cwd=dest, capture_output=True,
                              env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/tmp",
                                   # never let git discover an enclosing repository around the data dir
                                   "GIT_CEILING_DIRECTORIES": str(dest.parent)})
        if proc.returncode != 0:
            return None, proc.stderr[-800:]
        return dest, ""

    def _verify(self, patched: Path) -> None:
        """Three sandbox runs in parallel: the reproduction test alone on HEAD+fix (the fix's own
        verdict), and the project's full suite on HEAD and on HEAD+fix (to find new failures). If the
        suite cannot even be collected at untouched HEAD, say so instead of blaming the fix."""
        st = self.state
        self.phase("verify")
        self.log("Running the reproduction test on HEAD+fix, and the full suite on HEAD and HEAD+fix, in parallel.")
        base = self.sandbox.snapshot(self.repo.path, st.bad_sha, self.snap_key)
        timeout = self.test_timeout()

        def run(snap: Path, repro_only: bool) -> TestRun:
            key, ok = self._deps_key(snap)
            if not ok:
                return TestRun(sha="", status="install_failed", exit_code=None, duration_s=0, output_tail="")
            if repro_only:
                return self.sandbox.run_pytest(snap, st.bad_sha, key, inject={st.repro["path"]: st.repro["code"]},
                                               targets=[st.repro["path"]], timeout_s=timeout)
            return self.sandbox.run_pytest(snap, st.bad_sha, key, timeout_s=timeout)

        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            f_repro, f_base, f_fix = ex.submit(run, patched, True), ex.submit(run, base, False), ex.submit(run, patched, False)
            repro, baseline, fixed = f_repro.result(), f_base.result(), f_fix.result()

        def failing(r: TestRun) -> set[str]:
            return {c.nodeid for c in r.cases if c.outcome in ("failed", "error")}

        repro_passes = repro.status == "passed"
        # A suite that errors during collection at untouched HEAD (e.g. it needs the package pip-installed
        # for entry points) can't judge the fix; don't count its errors against it.
        suite_ok = baseline.status in ("passed", "failed") and bool(baseline.cases)
        new_failures = sorted(failing(fixed) - failing(baseline)) if suite_ok else []
        st.fix["verification"] = {
            "repro_passes": repro_passes,
            "suite_available": suite_ok,
            "baseline": {"status": baseline.status, "total": len(baseline.cases), "failing": len(failing(baseline)),
                         "duration_s": baseline.duration_s},
            "patched": {"status": fixed.status, "total": len(fixed.cases), "failing": len(failing(fixed)),
                        "duration_s": fixed.duration_s},
            "new_failures": new_failures[:30],
            "output_tail": (fixed if suite_ok else baseline).output_tail[-3000:],
        }
        ok_label = "Revert verified" if st.fix.get("fallback") == "revert" else "Fix verified"
        if not repro_passes:
            verdict = "Fix needs work"
        elif not suite_ok:
            verdict = f"{ok_label} (reproduction only)"
        elif new_failures:
            verdict = ("Revert" if st.fix.get("fallback") == "revert" else "Fix") + " introduces new failures"
        else:
            verdict = ok_label
        st.fix["verdict"] = verdict
        suite_note = (f"{len(new_failures)} new failures across {len(fixed.cases)} tests" if suite_ok else
                      f"the project's own suite could not run at untouched HEAD either ({baseline.status})")
        self.log(f"{verdict}: reproduction {'passes' if repro_passes else 'still fails'}; {suite_note}.")
        self.emit("verification", **st.fix["verification"], verdict=verdict)

def _replace_once(text: str, find: str, replace: str) -> str | None:
    """Replace a snippet that occurs exactly once; tolerate trailing-whitespace and
    indentation-of-blank-line differences, which models commonly get wrong."""
    if find and text.count(find) == 1:
        return text.replace(find, replace, 1)
    norm = lambda t: "\n".join(line.rstrip() for line in t.split("\n"))  # noqa: E731
    lines, flines = text.split("\n"), norm(find).strip("\n").split("\n")
    if not find.strip() or not flines:
        return None
    hits = [i for i in range(len(lines) - len(flines) + 1)
            if [x.rstrip() for x in lines[i:i + len(flines)]] == flines]
    if len(hits) == 1:
        i = hits[0]
        return "\n".join(lines[:i] + replace.strip("\n").split("\n") + lines[i + len(flines):])
    span = _locate_lines(text, find)
    if span is None:
        return None
    a, b = span
    return "\n".join(lines[:a] + replace.strip("\n").split("\n") + lines[b + 1:])


_NON_BEHAVIOURAL = {"_version.py", "version.py", "__version__.py", "__about__.py", "_meta.py", "__meta__.py", "about.py"}


def _is_code(path: str) -> bool:
    """Library source that can change behaviour. Version stamps, tests and conftest files are
    Python too, but never explain a regression in the library itself."""
    parts = path.split("/")
    name = parts[-1]
    if not name.endswith((".py", ".pyx", ".pyi")) or name in _NON_BEHAVIOURAL:
        return False
    is_test = (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
               or any(p in ("tests", "test", "testing") for p in parts[:-1]))
    return not is_test


_TOKEN = re.compile(r"[^\s#]+")


def _case_name(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def _candidate_table(head: TestRun, good: TestRun) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Per-candidate outcomes at HEAD and known-good, and the candidates that reproduce the bug:
    a real assertion failure at HEAD (not an ImportError-style breakage) and a pass at known-good."""
    good_by = {_case_name(c.nodeid): c for c in good.cases}
    table, winners = [], []
    for c in head.cases:
        name = _case_name(c.nodeid)
        g = good_by.get(name)
        g_out = g.outcome if g else f"not collected ({good.status})"
        h_out = c.outcome
        if c.outcome == "failed" and _NOT_A_SYMPTOM.search(c.message):
            h_out = "broken (not the symptom)"
        table.append((name, h_out, g_out, c.message[:160]))
        if h_out == "failed" and g is not None and g.outcome == "passed":
            winners.append(name)
    return table, winners


def _keep_tests(code: str, keep: list[str]) -> str:
    """Drop candidate test functions that did not reproduce the bug. Parses the file with `ast`
    (nothing is executed); module-level imports and helpers are kept as they are."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    lines = code.splitlines(keepends=True)
    drop: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test") \
                and node.name not in keep:
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            drop.append((first - 1, node.end_lineno))
    if not drop:
        return code
    for a, b in sorted(drop, reverse=True):
        del lines[a:b]
    return re.sub(r"\n{4,}", "\n\n\n", "".join(lines))


def _counterfactual_status(pytest_status: str) -> str:
    """Only a clean assertion failure counts as evidence against causation; errors, timeouts
    and install failures say nothing either way."""
    return {"passed": "confirmed", "failed": "not_confirmed"}.get(pytest_status, "inconclusive")


def _locate_lines(text: str, find: str) -> tuple[int, int] | None:
    """Find the unique run of whole lines whose tokens equal the snippet's, ignoring all whitespace,
    line wrapping and comment markers. Handles code that was re-wrapped or reformatted after the
    model last saw it (e.g. a later "code cleanup" commit). Returns (first line, last line)."""
    target = _TOKEN.findall(find)
    if len(target) < 3:
        return None
    toks: list[tuple[str, int]] = [(t, n) for n, line in enumerate(text.split("\n")) for t in _TOKEN.findall(line)]
    words = [t for t, _ in toks]
    hits = [i for i in range(len(words) - len(target) + 1) if words[i:i + len(target)] == target]
    if len(hits) != 1:
        return None
    i = hits[0]
    return toks[i][1], toks[i + len(target) - 1][1]


def _closest_region(text: str, find: str, context: int = 3) -> str:
    """The lines of `text` that best match `find`, to show the model what is really there."""
    lines = text.split("\n")
    first = next((ln.strip() for ln in find.split("\n") if ln.strip()), "")
    if not first:
        return ""
    best = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.5)
    if not best:
        return ""
    i = [ln.strip() for ln in lines].index(best[0])
    n = len(find.strip("\n").split("\n"))
    return "\n".join(lines[max(0, i - context): i + n + context])


def _normalise_patch(patch: str) -> str:
    patch = patch.strip()
    m = re.search(r"```(?:diff|patch)?\n(.*?)```", patch, re.S)
    if m:
        patch = m.group(1)
    return patch.strip("\n") + "\n" if patch else ""


_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _repo_lock(url: str) -> threading.Lock:
    with _repo_locks_guard:
        return _repo_locks.setdefault(url, threading.Lock())


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]
