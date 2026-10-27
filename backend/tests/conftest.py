import subprocess
from pathlib import Path

import pytest

from bisect_agent.config import Settings
from bisect_agent.gitops import Repo


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/tmp"})


GOOD_IMPL = '''
def split_fields(line):
    """Split a CSV-ish line, honouring double quotes."""
    out, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return out
'''

BAD_IMPL = '''
def split_fields(line):
    """Split a CSV-ish line. (Simplified: faster!)"""
    return [f.strip('"') for f in line.split(",")]
'''


def build_fixture_repo(root: Path, n: int = 40, culprit: int = 23) -> Path:
    """A tiny pure-Python package with a regression introduced at commit `culprit`.
    Commit 30 also adds a dependency so the install path gets exercised."""
    src = root / "fixture-src"
    (src / "csvlite").mkdir(parents=True)
    (src / "tests").mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "pyproject.toml").write_text('[project]\nname="csvlite"\nversion="0.1"\ndependencies=[]\n'
                                        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')
    (src / "tests" / "test_basic.py").write_text("from csvlite import split_fields\n\ndef test_simple():\n    assert split_fields('a,b') == ['a', 'b']\n")
    for i in range(n):
        impl = BAD_IMPL if i >= culprit else GOOD_IMPL
        (src / "csvlite" / "__init__.py").write_text("from ._version import VERSION\n" + impl)
        (src / "csvlite" / "_version.py").write_text(f"VERSION = {i}\n")
        if i == 30:
            (src / "pyproject.toml").write_text('[project]\nname="csvlite"\nversion="0.1"\ndependencies=["six==1.16.0"]\n'
                                                '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "Simplify field splitting" if i == culprit else f"Commit {i}")
        if i % 7 == 3:
            (src / "README.md").write_text(f"docs revision {i}\n")
            _git(src, "add", "-A")
            _git(src, "commit", "-q", "-m", f"docs: revision {i}")
    return src


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory):
    root = Path.cwd() / ".." / "data" / "test-fixtures"
    root = root.resolve()
    subprocess.run(["rm", "-rf", str(root)])
    root.mkdir(parents=True)
    src = build_fixture_repo(root)
    bare = root / "repos" / "csvlite"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return Repo(bare, "https://github.com/example/csvlite.git")


@pytest.fixture(scope="session")
def test_settings(fixture_repo):
    import os
    os.environ["BISECT_DATA_DIR"] = str(fixture_repo.path.parent.parent)
    return Settings()
