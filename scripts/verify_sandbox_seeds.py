"""No-AI proof that the sandbox + git bisect driver find each seed's culprit using a
hand-written reproduction test. Usage: python scripts/verify_sandbox_seeds.py [seed-id ...]"""
import json
import sys
import time
from pathlib import Path

from bisect_agent.config import settings
from bisect_agent.gitops import BisectSession, Repo, repo_slug, validate_repo_url
from bisect_agent.sandbox import Sandbox

ROOT = Path(__file__).resolve().parents[1]


def resolve(repo: Repo, ref: str) -> str:
    sha = repo.rev(ref)
    if not sha:
        repo.git("fetch", "--quiet", "origin", ref, check=False, timeout=300)
        sha = repo.rev(ref) or repo.rev("FETCH_HEAD")
    return sha


def main(ids):
    s = settings()
    seeds = [x for x in json.loads((ROOT / "seeds/catalog.json").read_text()) if not ids or x["id"] in ids]
    ok_all = True
    for seed in seeds:
        t0 = time.time()
        sb = Sandbox(s, python=seed["python"])
        sb.ensure_ready()
        repo = Repo.clone(seed["repo_url"], s.repos_dir)
        key = repo_slug(validate_repo_url(seed["repo_url"]))
        good, bad = resolve(repo, seed["good_ref"]), resolve(repo, seed["bad_ref"])
        test = seed["handwritten_test"]

        def run(sha):
            snap = sb.snapshot(repo.path, sha, key)
            dk, ok, log = sb.install(*sb.requirements_for(snap))
            if not ok:
                return "install_failed", log[-300:]
            r = sb.run_pytest(snap, sha, dk, inject={"test_bisect_repro.py": test}, targets=["test_bisect_repro.py"])
            return r.status, r.output_tail[-300:]

        print(f"\n### {seed['id']}  good={good[:10]} bad={bad[:10]} commits={repo.count_between(good, bad)}")
        for label, sha in (("good", good), ("bad", bad)):
            st, tail = run(sha)
            print(f"  {label:4} {sha[:10]} -> {st}")
            if (label == "good") != (st == "passed"):
                print("    " + tail.replace("\n", "\n    "))
        session = BisectSession(repo)
        status = session.start(bad, good)
        steps = 0
        while not status.done and steps < 15:
            sha = status.next_sha
            st, _ = run(sha)
            verdict = {"passed": "good", "failed": "bad"}.get(st, "skip")
            print(f"  step {steps + 1:2} {sha[:10]} {st:8} -> {verdict}")
            status = session.mark(sha, verdict)
            steps += 1
        session.reset()
        found = status.first_bad or (status.candidates and f"ambiguous {status.candidates}")
        ok = status.first_bad == seed["expected"]
        ok_all &= ok
        print(f"  RESULT {'PASS' if ok else 'FAIL'}: found {found}, expected {seed['expected'][:10]} "
              f"({steps} steps, {time.time() - t0:.0f}s)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
