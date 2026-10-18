"""Day 1-3 proof: sandbox + real git bisect, no AI."""
import pytest

from bisect_agent.gitops import BisectSession
from bisect_agent.sandbox import Sandbox

REPRO = '''
from csvlite import split_fields

def test_quoted_comma():
    assert split_fields('a,"b,c",d') == ["a", "b,c", "d"]
'''

pytestmark = pytest.mark.docker


def verdict(run):
    return {"passed": "good", "failed": "bad"}.get(run.status, "skip")


def test_sandbox_runs_and_isolates(fixture_repo, test_settings):
    sb = Sandbox(test_settings)
    sb.ensure_ready()
    head = fixture_repo.rev("main")
    snap = sb.snapshot(fixture_repo.path, head)
    reqs, cons = sb.requirements_for(snap)
    assert reqs == ["six==1.16.0"] and cons == []
    key, ok, log = sb.install(reqs, cons)
    assert ok, log
    probe = '''
import socket, six, os
def test_no_network():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        raise AssertionError("network reachable")
    except OSError:
        pass
def test_src_readonly():
    try:
        open("/src/x", "w")
        raise AssertionError("src writable")
    except OSError:
        pass
'''
    run = sb.run_pytest(snap, head, key, inject={"tests/test_probe.py": probe}, targets=["tests/test_probe.py"])
    assert run.status == "passed", run.output_tail
    assert len(run.cases) == 2


def test_timeout_is_enforced(fixture_repo, test_settings):
    sb = Sandbox(test_settings)
    head = fixture_repo.rev("main")
    snap = sb.snapshot(fixture_repo.path, head)
    key, ok, _ = sb.install(["six==1.16.0"])
    run = sb.run_pytest(snap, head, key, inject={"t/test_slow.py": "import time\ndef test_x():\n    time.sleep(60)\n"},
                        targets=["t/test_slow.py"], timeout_s=5)
    assert run.status == "timeout"
    assert run.duration_s < 30


def test_bisect_finds_culprit(fixture_repo, test_settings):
    sb = Sandbox(test_settings)
    sb.ensure_ready()
    head = fixture_repo.rev("main")
    root = fixture_repo.git("rev-list", "--max-parents=0", "main").strip()
    session = BisectSession(fixture_repo)
    status = session.start(head, root)
    steps = 0
    while not status.done:
        sha = status.next_sha
        snap = sb.snapshot(fixture_repo.path, sha)
        key, ok, _ = sb.install(*sb.requirements_for(snap))
        run = sb.run_pytest(snap, sha, key, inject={"tests/test_repro.py": REPRO}, targets=["tests/test_repro.py"])
        status = session.mark(sha, verdict(run))
        steps += 1
        assert steps <= 15
    session.reset()
    assert fixture_repo.commit(status.first_bad).subject == "Simplify field splitting"
