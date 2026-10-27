"""Summarise the exported seed runs as markdown tables (used for the README and Devpost numbers).
Usage: python scripts/seed_report.py [seeds/runs dir]"""
import json
import statistics as stats
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runs_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "seeds" / "runs"
catalog = {s["id"]: s for s in json.loads((ROOT / "seeds" / "catalog.json").read_text())}

rows, calls = [], []
for f in sorted(runs_dir.glob("*.json")):
    st = json.loads(f.read_text())["state"]
    seed = catalog.get(st["id"], {})
    fb, cf, fix = st.get("first_bad", {}), st.get("counterfactual", {}), st.get("fix", {})
    v = fix.get("verification") or {}
    calls += st.get("calls", [])
    found = "✅" if fb.get("sha") == seed.get("expected") else f"❌ {fb.get('sha', '')[:7]}"
    proof = {"confirmed": "✅ confirmed", "not_confirmed": "❌ not confirmed", "inconclusive": "⚪ inconclusive",
             "conflict": "⚪ revert conflicts"}.get(cf.get("status"), "—")
    if cf.get("method") == "model_resolved" and cf.get("status") == "confirmed":
        proof += " (Ultra-resolved revert)"
    verdict = fix.get("verdict", "—")
    if v.get("suite_available") and verdict:
        verdict += f" — {v['patched']['total'] - v['patched']['failing']}/{v['patched']['total']} suite tests pass, {len(v.get('new_failures', []))} new failures"
    wall = (st.get("finished_at", 0) - st.get("created_at", 0)) / 60
    econ = st.get("economics", {})
    rows.append(
        f"| [{seed.get('title', st['id']).split(':')[0]}]({seed.get('issue_url', '')}) | {found} `{fb.get('sha', '')[:7]}` "
        f"| {len(st.get('steps', []))} / {st.get('range_size', 0)} | {proof} | {verdict} | {wall:.1f} min | "
        f"${econ.get('actual_usd', 0):.3f} |")

print("| Seed | Culprit found | Steps / commits in range | Causal proof | Fix | Wall time | Model cost |")
print("|---|---|---|---|---|---|---|")
print("\n".join(rows))

print("\n| Tier | Task | Calls | Median latency | Max latency | Median output tokens | of which thinking |")
print("|---|---|---|---|---|---|---|")
by: dict[tuple[str, str], list[dict]] = {}
for c in calls:
    by.setdefault((c["tier"], c["task"]), []).append(c)
for (tier, task), cs in sorted(by.items(), key=lambda kv: (kv[0][0] != "fast", kv[0][1])):
    lat = [c["latency_s"] for c in cs]
    out = [c["completion_tokens"] for c in cs]
    think = [c.get("reasoning_tokens", 0) for c in cs]
    print(f"| {'Nano' if tier == 'fast' else 'Ultra'} | `{task}` | {len(cs)} | {stats.median(lat):.1f} s | {max(lat):.1f} s "
          f"| {int(stats.median(out)):,} | {int(stats.median(think)):,} |")
errors = sum(1 for c in calls if not c.get("ok", True))
actual = sum(c.get("cost_usd", 0) for c in calls)
fast_share = sum(1 for c in calls if c["tier"] == "fast") / max(1, len(calls))
print(f"\ncalls={len(calls)} errors={errors} total_cost=${actual:.3f} fast_share={fast_share:.0%}")
