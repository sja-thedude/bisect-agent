"""Create a UI preview run on the local fixture repo using a fake Token Factory transport.
For UI development only: no real models are called and the run is labelled as a preview."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("BISECT_DATA_DIR", str(ROOT / "data" / "preview"))

from bisect_agent.agent import Agent, JobInput, JobState  # noqa: E402
from bisect_agent.config import settings  # noqa: E402
from bisect_agent.gitops import Repo  # noqa: E402
from bisect_agent.nebius import TokenFactory  # noqa: E402
from bisect_agent.router import Router  # noqa: E402
from bisect_agent.store import Store  # noqa: E402
from tests.conftest import build_fixture_repo  # noqa: E402
from tests.test_agent_offline import fake_transport  # noqa: E402
import subprocess, shutil  # noqa: E402

s = settings()
fx = s.data_dir / "fixture"
shutil.rmtree(fx, ignore_errors=True)
fx.mkdir(parents=True)
src = build_fixture_repo(fx)
bare = fx / "csvlite.git"
subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
repo = Repo(bare, "https://github.com/example/csvlite.git")
Repo.clone = classmethod(lambda cls, url, d: Repo(bare, url))
store = Store(s.db_path)
st = JobState(id="preview", input=JobInput(repo_url="https://github.com/example/csvlite",
              bug_report="[UI PREVIEW, fake models] Quoted fields containing commas are split into separate fields.",
              good_ref=repo.git("rev-list", "--max-parents=0", "main").strip()))
store.create_run(st.to_dict(), seed=True, title="UI preview (fixture repo, fake models)")


def emit(k, p):
    store.add_event(st.id, k, p)
    store.save_state(st.to_dict())


Agent(s, st, emit, router=Router(TokenFactory(api_key="x", transport=fake_transport(
    repo.file_at(repo.rev("main"), "csvlite/__init__.py"))))).run()
store.save_state(st.to_dict())
print(st.outcome, st.first_bad.get("subject"))
