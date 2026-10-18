"""Command line: run a job headless, export seeds, check Token Factory, or serve the UI."""
from __future__ import annotations

import argparse
import json
import sys
import time

from .agent import Agent, JobInput, JobState, new_job_id
from .config import settings
from .nebius import TokenFactory
from .router import MODELS, Router, Tier
from .store import Store


def _print_event(kind: str, p: dict) -> None:
    ts = time.strftime("%H:%M:%S")
    if kind == "log":
        print(f"{ts}  {p['message']}")
    elif kind == "phase":
        print(f"{ts}  == {p['phase']} ==")
    elif kind == "call":
        print(f"{ts}    [{p['tier']:6}] {p['task']:<26} {p['model']:<40} {p['latency_s']:>6.1f}s "
              f"{p['prompt_tokens']:>6}+{p['completion_tokens']:<5} tok {'ok' if p['ok'] else 'ERR ' + p['error'][:80]}")
    elif kind == "step":
        print(f"{ts}  step {p['index']:>2}  {p['sha'][:10]}  {p['verdict']:<4}  {p['status']:<8} {p['duration_s']:>5.1f}s"
              f"  {p['subject'][:60]}{'  (prefetched)' if p['speculative_hit'] else ''}")
    elif kind == "search":
        print(f"{ts}    [tavily] {p['purpose']}: {p['query']} -> {len(p['hits'])} hits")
    elif kind == "first_bad":
        print(f"{ts}  FIRST BAD: {p['sha']} {p['subject']}\n           {p['url']}")


def cmd_run(a: argparse.Namespace) -> int:
    s = settings()
    store = Store(s.db_path)
    state = JobState(id=a.id or new_job_id(), input=JobInput(
        repo_url=a.repo, bug_report=a.bug, good_ref=a.good, bad_ref=a.bad, python=a.python, propose_fix=not a.no_fix))
    store.create_run(state.to_dict(), seed=bool(a.export), title=a.title or "")

    def emit(kind: str, payload: dict) -> None:
        store.add_event(state.id, kind, payload)
        store.save_state(state.to_dict())
        if not a.quiet:
            _print_event(kind, payload)

    Agent(s, state, emit).run()
    store.save_state(state.to_dict())
    fb = state.first_bad
    print(json.dumps({"id": state.id, "outcome": state.outcome, "first_bad": fb.get("sha"), "subject": fb.get("subject"),
                      "steps": len(state.steps), "limits_hit": state.limits_hit, "error": state.error,
                      "headline": state.explanation.get("headline"), "fix": state.fix.get("verdict"),
                      "models": state.model_totals}, indent=2))
    if a.export:
        data = store.export(state.id)
        data["title"] = a.title or ""
        with open(a.export, "w") as f:
            json.dump(data, f, indent=1)
        print(f"exported to {a.export}")
    if a.expect and not (fb.get("sha") or "").startswith(a.expect):
        print(f"EXPECTED first bad commit {a.expect}, got {fb.get('sha')}", file=sys.stderr)
        return 1
    return 0 if state.outcome == "found" else 2


def cmd_models(a: argparse.Namespace) -> int:
    tf = TokenFactory()
    available = tf.list_models()
    nemotron = [m for m in available if "nemotron" in m.lower()]
    print("Nemotron models on Token Factory:\n  " + "\n  ".join(nemotron))
    r = Router(tf)
    for tier, model in MODELS.items():
        status = "available" if model in available else "NOT LISTED"
        print(f"\n{tier.value:6} -> {model} ({status})")
        task = "summarise_diff" if tier is Tier.FAST else "explain_culprit"
        res = r.call(task, "Reply with JSON only.", 'Reply with {"pong": true}.', json_out=True)
        print(f"         ping: {res.data} in {res.record.latency_s}s, "
              f"{res.record.prompt_tokens}+{res.record.completion_tokens} tokens")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("bisect_agent.server:app", host=a.host, port=a.port, log_level="info")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="bisect-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="hunt a regression")
    r.add_argument("--repo", required=True)
    r.add_argument("--bug", required=True, help="plain-English bug report")
    r.add_argument("--good", help="known-good ref (default: 200 commits back)")
    r.add_argument("--bad", help="bad ref (default: HEAD)")
    r.add_argument("--python", default="3.11")
    r.add_argument("--no-fix", action="store_true")
    r.add_argument("--export", help="write state+events JSON (for seeding the demo)")
    r.add_argument("--title", help="title for an exported seed")
    r.add_argument("--id", help="fixed run id (seeds keep stable permalinks)")
    r.add_argument("--expect", help="exit non-zero unless the first bad commit starts with this sha")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)
    m = sub.add_parser("models", help="check Token Factory access and both tiers")
    m.set_defaults(fn=cmd_models)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
