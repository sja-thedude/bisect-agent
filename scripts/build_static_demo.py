"""Bake the verified seed runs into the web build so the UI works as a static site (no backend).
Writes web/public/demo/{config.json,index.json,runs/<id>.json}. Run before `next build` with
NEXT_PUBLIC_STATIC_DEMO=1. Live runs still need the full app (backend + Docker)."""
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from bisect_agent.config import Limits  # noqa: E402
from bisect_agent.router import MODELS, ROUTES, TIER_PARAMS  # noqa: E402
from bisect_agent.sandbox import SUPPORTED_PYTHONS  # noqa: E402

out = ROOT / "web" / "public" / "demo"
shutil.rmtree(out, ignore_errors=True)
(out / "runs").mkdir(parents=True)
index = []
for f in sorted((ROOT / "seeds" / "runs").glob("*.json")):
    data = json.loads(f.read_text())
    st = data["state"]
    st["seed"], st["title"] = True, data.get("title", "")
    (out / "runs" / f"{st['id']}.json").write_text(json.dumps(data))
    index.append({"id": st["id"], "created_at": st["created_at"], "finished_at": st.get("finished_at"),
                  "repo_url": st["input"]["repo_url"], "bug_report": st["input"]["bug_report"], "phase": st["phase"],
                  "outcome": st.get("outcome", ""), "seed": 1, "title": st["title"],
                  "first_bad_sha": st.get("first_bad", {}).get("sha"), "first_bad_subject": st.get("first_bad", {}).get("subject")})
(out / "index.json").write_text(json.dumps(index))
(out / "config.json").write_text(json.dumps({
    "provider": "Nebius Token Factory", "base_url": "https://api.tokenfactory.nebius.com/v1",
    "tiers": {t.value: {"model": m, **TIER_PARAMS[t]} for t, m in MODELS.items()},
    "routes": {task: tier.value for task, tier in ROUTES.items()},
    "limits": asdict(Limits()), "nebius_configured": False, "tavily_configured": False,
    "pythons": list(SUPPORTED_PYTHONS), "static": True,
}))
print(f"baked {len(index)} runs into {out}")
