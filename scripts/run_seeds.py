"""Run the agent (real Nemotron calls) on every seed in seeds/catalog.json, check that it found
the expected culprit, and export the run to seeds/runs/<id>.json for the demo.
Usage: python scripts/run_seeds.py [seed-id ...]"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "bisect_agent.cli"]


def main(ids):
    seeds = [s for s in json.loads((ROOT / "seeds/catalog.json").read_text()) if not ids or s["id"] in ids]
    results = {}
    for s in seeds:
        out = ROOT / "seeds" / "runs" / f"{s['id']}.json"
        cmd = CLI + ["run", "--repo", s["repo_url"], "--bug", s["bug_report"], "--good", s["good_ref"],
                     "--bad", s["bad_ref"], "--python", s["python"], "--id", s["id"], "--title", s["title"],
                     "--expect", s["expected"], "--export", str(out)]
        print(f"\n######## {s['id']}", flush=True)
        rc = subprocess.run(cmd, cwd=ROOT / "backend").returncode
        results[s["id"]] = "PASS" if rc == 0 else f"FAIL (exit {rc})"
    print("\n" + "\n".join(f"{k}: {v}" for k, v in results.items()))
    return 0 if all(v == "PASS" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
