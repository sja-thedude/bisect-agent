"""The Bisect agent loop.

The job's entire state lives in one ``JobState`` dataclass that is serialised after every
change, so a run can be inspected (and replayed in the UI) at any point.

Phases:
  clone -> reproduce -> confirm -> bisect -> (research) -> explain -> fix -> verify -> done
Any phase can end the job early with partial findings when a hard limit is hit.
"""
from __future__ import annotations

import concurrent.futures as cf
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
from .router import CallRecord, Router
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
        st = self.state
        self.phase("reproduce")
        context = self._gather_context()
        st.repro["path"] = self._repro_path()
        user = (f"BUG REPORT:\n{st.input.bug_report}\n\nThe test file will be written to `{st.repro['path']}`.\n\n"
                f"REPOSITORY CONTEXT:\n{context}")
        res = self.router.call("write_repro_test", prompts.WRITE_REPRO_TEST, user, json_out=True)
        self._set_repro(res.data, attempt=1)

        for attempt in range(1, MAX_REPRO_ATTEMPTS + 1):
            self.check_time(60)
            self.phase("confirm")
            self.log(f"Running reproduction test at HEAD {st.bad_sha[:10]} (attempt {attempt})")
            head = self._run_repro(st.bad_sha)
            st.head_run = _sanitize(head)
            problem = self._head_problem(head)
            if not problem:
                self.log(f"Reproduction test fails at HEAD as expected ({head.duration_s}s).")
                self.log(f"Running it at known-good {st.good_sha[:10]}")
                good = self._run_repro(st.good_sha)
                st.good_run = _sanitize(good)
                if good.status == "passed":
                    self.log(f"Passes at known-good ({good.duration_s}s). Reproduction confirmed.")
                    self.emit("repro_confirmed", head=st.head_run, good=st.good_run)
                    return
                problem = ("it must PASS at the known-good commit, but there it gave status "
                           f"{good.status!r}", good)
            if attempt == MAX_REPRO_ATTEMPTS:
                break
            msg, run = problem
            self.log(f"Reproduction not confirmed: {msg}. Asking for a revised test.", "warn")
            self.phase("reproduce")
            where = "HEAD (expected to fail)" if run.sha == st.bad_sha else "the known-good commit (expected to pass)"
            repair_user = (f"BUG REPORT:\n{st.input.bug_report}\n\nCURRENT TEST ({st.repro['path']}):\n"
                           f"```python\n{st.repro['code']}\n```\n\nRAN AT: {where}\nPROBLEM: {msg}\n\n"
                           f"PYTEST OUTPUT:\n{run.output_tail[-5000:]}\n\nREPOSITORY CONTEXT:\n{context[:60000]}")
            if run.sha == st.good_sha:
                repair_user += self._good_ref_context()
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

    def _head_problem(self, run: TestRun) -> tuple[str, TestRun] | None:
        failed = [c for c in run.cases if c.outcome == "failed"]
        if run.status == "failed" and failed:
            broken = [c for c in failed if _NOT_A_SYMPTOM.search(c.message)]
            if len(broken) == len(failed):
                return (f"it failed at HEAD, but with {broken[0].message[:200]!r}, which is a broken test, "
                        "not the reported behaviour", run)
            return None
        if run.status == "passed":
            return "it PASSED at HEAD, so it does not capture the bug", run
        return f"it must FAIL with an assertion at HEAD, but pytest reported {run.status!r} (not an assertion failure)", run

    # -------------------------------------------------------------- bisect
    def _classify(self, run: TestRun) -> tuple[str, str, CallRecord | None]:
        """Cheap tier reads the pytest output; hard evidence overrides it when they disagree."""
        st = self.state
        user = (f"BUG SYMPTOM: {st.repro.get('symptom', '')}\n\nREFERENCE FAILURE AT HEAD:\n"
                f"{st.head_run.get('output_tail', '')[-2500:]}\n\nTHIS COMMIT ({run.sha[:10]}): pytest status={run.status}, "
                f"exit={run.exit_code}\n{run.output_tail[-3500:]}")
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

        parent = st.first_bad["sha"] + "^"
        self.log("Explaining the culprit commit with the reasoning tier.")
        culprit_run = next((s for s in st.steps if s.sha == sha), None)
        user = (f"BUG REPORT:\n{st.input.bug_report}\n\nREPRODUCTION TEST:\n```python\n{st.repro['code']}\n```\n\n"
                f"FIRST BAD COMMIT: {sha} {st.first_bad['subject']}\nPytest at first bad commit: "
                f"{culprit_run.status if culprit_run else 'failed'}\nReference failure output:\n"
                f"{st.head_run.get('output_tail', '')[-3000:]}\n\nPARENT {parent}: test passed.\n\n"
                f"{findings_text}\n\nDIFF OF FIRST BAD COMMIT:\n{diff}")
        res = self.router.call("explain_culprit", prompts.EXPLAIN_CULPRIT, user, json_out=True, context=sha)
        st.explanation = res.data or {}
        self.emit("explanation", **st.explanation)

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
        st = self.state
        self.check_time(120)
        self.phase("fix")
        sha = st.first_bad["sha"]
        files = [f for f in st.first_bad.get("files", []) if self.repo.file_at(st.bad_sha, f) is not None][:8]
        head_files = "\n\n".join(f"===== {f} (at HEAD) =====\n{self.repo.file_at(st.bad_sha, f, 30000)}" for f in files)
        base_user = (f"BUG REPORT:\n{st.input.bug_report}\n\nEXPLANATION:\n{st.explanation}\n\n"
                     f"REPRODUCTION TEST ({st.repro['path']}):\n```python\n{st.repro['code']}\n```\n\n"
                     f"CULPRIT DIFF:\n{self.repo.diff(sha, 30000)}\n\nHEAD FILES:\n{head_files}")
        user = base_user
        for attempt in range(1, 3):
            res = self.router.call("propose_fix", prompts.PROPOSE_FIX, user, json_out=True)
            patch = _normalise_patch((res.data or {}).get("diff", ""))
            st.fix = {"diff": patch, "rationale": (res.data or {}).get("rationale", ""), "attempt": attempt}
            self.emit("fix", diff=patch, rationale=st.fix["rationale"])
            patched, err = self._apply_patch(patch)
            if patched:
                break
            self.log(f"Proposed patch did not apply cleanly: {err[:200]}", "warn")
            st.fix["apply_error"] = err
            user = base_user + f"\n\nYOUR PREVIOUS DIFF FAILED `git apply`:\n{patch}\n\nERROR:\n{err}\nProduce a diff that applies."
        else:
            return
        st.fix["applied"] = True
        self._verify(patched)

    def _apply_patch(self, patch: str) -> tuple[Path | None, str]:
        st = self.state
        if not patch.strip():
            return None, "empty diff"
        base = self.sandbox.snapshot(self.repo.path, st.bad_sha, self.snap_key)
        dest = self.s.data_dir / "jobs" / st.id / "patched"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(base, dest, symlinks=True)
        # Applying a patch is pure file manipulation on the host; nothing from the repo executes.
        proc = subprocess.run(["git", "apply", "--recount", "--whitespace=nowarn", "-"], input=patch, text=True,
                              cwd=dest, capture_output=True,
                              env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/tmp"})
        if proc.returncode != 0:
            return None, proc.stderr[-800:]
        return dest, ""

    def _verify(self, patched: Path) -> None:
        st = self.state
        self.phase("verify")
        self.log("Running the full test suite on HEAD and on HEAD+fix, plus the reproduction test.")
        base = self.sandbox.snapshot(self.repo.path, st.bad_sha, self.snap_key)
        timeout = self.test_timeout()

        def suite(snap: Path, inject: bool) -> TestRun:
            key, ok = self._deps_key(snap)
            if not ok:
                return TestRun(sha="", status="install_failed", exit_code=None, duration_s=0, output_tail="")
            return self.sandbox.run_pytest(snap, st.bad_sha, key,
                                           inject={st.repro["path"]: st.repro["code"]} if inject else None,
                                           timeout_s=timeout)

        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_base = ex.submit(suite, base, False)
            f_fix = ex.submit(suite, patched, True)
            baseline, fixed = f_base.result(), f_fix.result()

        def failing(run: TestRun) -> set[str]:
            return {c.nodeid for c in run.cases if c.outcome in ("failed", "error")}

        repro_cases = [c for c in fixed.cases if REPRO_NAME.removesuffix(".py") in c.nodeid]
        repro_passes = bool(repro_cases) and all(c.outcome == "passed" for c in repro_cases)
        new_failures = sorted(failing(fixed) - failing(baseline) - {c.nodeid for c in repro_cases})
        st.fix["verification"] = {
            "repro_passes": repro_passes,
            "baseline": {"status": baseline.status, "total": len(baseline.cases), "failing": len(failing(baseline)),
                         "duration_s": baseline.duration_s},
            "patched": {"status": fixed.status, "total": len(fixed.cases), "failing": len(failing(fixed)),
                        "duration_s": fixed.duration_s},
            "new_failures": new_failures[:30],
            "output_tail": fixed.output_tail[-3000:],
        }
        verdict = ("Fix verified" if repro_passes and not new_failures else
                   "Fix needs work" if not repro_passes else "Fix introduces new failures")
        st.fix["verdict"] = verdict
        self.log(f"{verdict}: reproduction {'passes' if repro_passes else 'still fails'}, "
                 f"{len(new_failures)} new failures across {len(fixed.cases)} tests.")
        self.emit("verification", **st.fix["verification"], verdict=verdict)


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
