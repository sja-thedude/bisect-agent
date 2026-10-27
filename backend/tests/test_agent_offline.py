"""End-to-end agent run on the fixture repo with a fake Token Factory (no API key needed)."""
import difflib
import json

import httpx
import pytest

from bisect_agent import prompts
from bisect_agent.agent import Agent, JobInput, JobState
from bisect_agent.gitops import Repo
from bisect_agent.nebius import TokenFactory
from bisect_agent.router import MODELS, Router, Tier

from .conftest import BAD_IMPL, GOOD_IMPL

REPRO = '''from csvlite import split_fields


def test_quoted_comma_is_one_field():
    assert split_fields('a,"b,c",d') == ["a", "b,c", "d"]
'''


def fake_transport(head_init: str):
    fixed = head_init.replace(BAD_IMPL, GOOD_IMPL)
    fix_diff = "".join(difflib.unified_diff(head_init.splitlines(True), fixed.splitlines(True),
                                            "a/csvlite/__init__.py", "b/csvlite/__init__.py"))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system == prompts.SELECT_CONTEXT_FILES:
            out = {"source_files": ["csvlite/__init__.py"], "test_files": ["tests/test_basic.py"],
                   "package_import": "from csvlite import split_fields"}
        elif system == prompts.WRITE_REPRO_TEST:
            out = {"test_code": REPRO, "rationale": "r", "symptom": "quoted commas split into two fields"}
        elif system == prompts.CLASSIFY_VERDICT:
            out = {"verdict": "good" if "status=passed" in user else "bad", "reason": "fake"}
        elif system == prompts.SUMMARISE_DIFF:
            out = {"summary": "changes things", "relevant": False}
        elif system == prompts.EXPLAIN_CULPRIT:
            out = {"headline": "split() ignores quotes", "what_changed": "w", "causal_chain": ["a", "b"],
                   "why_tests_missed_it": "", "confidence": "high", "citations": []}
        elif system == prompts.PROPOSE_FIX:
            out = {"edits": [{"path": "csvlite/__init__.py", "find": BAD_IMPL.strip("\n"),
                              "replace": GOOD_IMPL.strip("\n")}], "rationale": "restore quote-aware splitting"}
        else:
            raise AssertionError(f"unexpected task: {system[:60]}")
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": json.dumps(out)}}],
            "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 50},
        })

    return httpx.MockTransport(handler)


@pytest.mark.docker
def test_agent_end_to_end(fixture_repo, test_settings, monkeypatch):
    head = fixture_repo.rev("main")
    head_init = fixture_repo.file_at(head, "csvlite/__init__.py")
    monkeypatch.setattr(Repo, "clone", classmethod(lambda cls, url, d: Repo(fixture_repo.path, url)))
    router = Router(TokenFactory(api_key="test", transport=fake_transport(head_init)))
    events = []
    state = JobState(id="offline1", input=JobInput(
        repo_url="https://github.com/example/csvlite", bug_report="Quoted fields containing commas are split apart.",
        good_ref=fixture_repo.git("rev-list", "--max-parents=0", "main").strip()))
    Agent(test_settings, state, lambda k, p: events.append((k, p)), router=router).run()

    assert state.error == "", state.error
    assert state.outcome == "found"
    assert state.first_bad["subject"] == "Simplify field splitting"
    assert len(state.steps) <= 15
    assert any(s.speculative_hit for s in state.steps), "speculative prefetch never hit"
    assert state.fix["verdict"] == "Fix verified", state.fix
    assert state.counterfactual["status"] == "confirmed", state.counterfactual
    assert state.economics["saved_pct"] > 0
    assert len(state.range_commits) == 39  # 46 commits, minus the known-good root and 6 docs-only commits
    tiers = {c["task"]: c["tier"] for c in state.calls}
    assert tiers["classify_verdict"] == "fast" and tiers["explain_culprit"] == "reason"
    models = {c["model"] for c in state.calls}
    assert models == {MODELS[Tier.REASON], MODELS[Tier.FAST]}
    kinds = [k for k, _ in events]
    assert kinds[-1] == "report" and "first_bad" in kinds
