"""Git plumbing and the bisect driver.

We drive the real ``git bisect`` (in ``--no-checkout`` mode on a bare clone) and only
feed it verdicts. Binary search, skip handling and the final answer are git's.
Nothing here executes repository code: hooks are disabled and we never check out
into a working tree on the host.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"}

# Commits that only touch these paths cannot change library behaviour; git bisect
# is told to ignore them via the pathspec. The leading ":(top)" include is required:
# `git bisect start` rejects exclude-only pathspecs even though `rev-list` accepts them.
IRRELEVANT_PATHSPEC = [
    ":(top)",
    ":(exclude,glob)**/*.md", ":(exclude,glob)**/*.rst", ":(exclude,glob)**/*.txt.in",
    ":(exclude)docs", ":(exclude)doc", ":(exclude).github", ":(exclude).gitignore",
    ":(exclude,glob)**/*.yml", ":(exclude,glob)**/*.yaml", ":(exclude)LICENSE",
    ":(exclude)AUTHORS", ":(exclude)CHANGES", ":(exclude)CHANGELOG",
]

DEPENDENCY_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|constraints[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|"
    r"Pipfile(\.lock)?|poetry\.lock|uv\.lock|pdm\.lock|tox\.ini)$"
)


# Isolated from the user's git config: no credential helpers, prompts or global hooks.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


class GitError(RuntimeError):
    pass


def validate_repo_url(url: str) -> str:
    url = url.strip()
    if url.startswith("git@"):
        raise GitError("use an https URL")
    p = urlparse(url if "://" in url else "https://" + url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS:
        raise GitError(f"only public https repos on {', '.join(sorted(ALLOWED_HOSTS))} are supported")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        raise GitError("URL must point at a repository, e.g. https://github.com/owner/repo")
    path = "/".join(parts[:2]).removesuffix(".git")
    return f"https://{p.hostname}/{path}.git"


def repo_slug(url: str) -> str:
    p = urlparse(url)
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", f"{p.hostname}/{p.path.strip('/').removesuffix('.git')}")


def web_commit_url(repo_url: str, sha: str) -> str:
    base = repo_url.removesuffix(".git")
    sep = "/-/commit/" if "gitlab.com" in base else "/commit/"
    return f"{base}{sep}{sha}"


@dataclass
class Commit:
    sha: str
    subject: str
    author: str
    date: str


class Repo:
    def __init__(self, path: Path, url: str):
        self.path = path
        self.url = url

    @property
    def name(self) -> str:
        return self.path.name

    def git(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, timeout=timeout,
            env=_GIT_ENV,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])}: {proc.stderr.strip()[-500:]}")
        return proc.stdout

    @classmethod
    def clone(cls, url: str, repos_dir: Path) -> "Repo":
        url = validate_repo_url(url)
        dest = repos_dir / repo_slug(url)
        repos_dir.mkdir(parents=True, exist_ok=True)
        repo = cls(dest, url)
        if (dest / "HEAD").exists():
            repo.git("fetch", "--quiet", "--prune", "--tags", "origin", "+refs/heads/*:refs/heads/*", timeout=600)
        else:
            proc = subprocess.run(
                ["git", "clone", "--quiet", "--bare", "-c", "core.hooksPath=/dev/null", url, str(dest)],
                capture_output=True, text=True, timeout=600, env=_GIT_ENV,
            )
            if proc.returncode != 0:
                raise GitError(f"clone failed: {proc.stderr.strip()[-500:]}")
        return repo

    # ----------------------------------------------------------------- queries
    def rev(self, ref: str) -> str:
        if ref.startswith("-"):
            raise GitError(f"bad ref {ref!r}")
        return self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()

    def commit(self, sha: str) -> Commit:
        out = self.git("show", "-s", "--format=%H%x00%s%x00%an%x00%cI", sha)
        h, s, a, d = out.strip().split("\x00")
        return Commit(h, s, a, d)

    def default_good(self, bad: str, n: int) -> str:
        """The commit n first-parent steps back from bad, or the root if history is shorter."""
        shas = self.git("rev-list", "--first-parent", f"--max-count={n + 1}", bad).split()
        return shas[-1]

    def count_between(self, good: str, bad: str, pathspec: list[str] | None = None) -> int:
        return int(self.git("rev-list", "--count", f"{good}..{bad}", "--", *(pathspec or [])).strip() or 0)

    def is_ancestor(self, a: str, b: str) -> bool:
        return subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", a, b],
                              capture_output=True, env=_GIT_ENV).returncode == 0

    def changed_files(self, sha: str) -> list[str]:
        out = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", "--root", sha)
        return [x for x in out.splitlines() if x]

    def diff(self, sha: str, max_chars: int = 60000, paths: list[str] | None = None) -> str:
        out = self.git("show", "--format=commit %H%nAuthor: %an%nDate: %cI%n%n%B", "--stat", "--patch",
                       "--first-parent", sha, "--", *(paths or []))
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n... [diff truncated, {len(out) - max_chars} more chars]"
        return out

    def file_at(self, sha: str, path: str, max_chars: int = 40000) -> str | None:
        proc = subprocess.run(["git", "-C", str(self.path), "show", f"{sha}:{path}"], capture_output=True, env=_GIT_ENV)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode(errors="replace")[:max_chars]

    def tree(self, sha: str) -> list[str]:
        return self.git("ls-tree", "-r", "--name-only", sha).splitlines()

    def log(self, good: str, bad: str, limit: int = 300) -> list[Commit]:
        out = self.git("log", f"--max-count={limit}", "--format=%H%x00%s%x00%an%x00%cI", f"{good}..{bad}")
        return [Commit(*line.split("\x00")) for line in out.splitlines() if line]


@dataclass
class BisectStatus:
    next_sha: str | None = None
    first_bad: str | None = None
    candidates: list[str] = field(default_factory=list)  # set when skips make the answer ambiguous
    raw: str = ""

    @property
    def done(self) -> bool:
        return self.next_sha is None


class BisectSession:
    """Thin wrapper over ``git bisect --no-checkout``. One session per bare repo at a time."""

    def __init__(self, repo: Repo, pathspec: list[str] | None = None):
        self.repo = repo
        self.pathspec = pathspec if pathspec is not None else IRRELEVANT_PATHSPEC

    def start(self, bad: str, good: str) -> BisectStatus:
        self.repo.git("bisect", "reset", check=False)
        out = self.repo.git("bisect", "start", "--no-checkout", bad, good, "--", *self.pathspec)
        return self._status(out)

    def mark(self, sha: str, verdict: str) -> BisectStatus:
        term = {"good": "good", "bad": "bad", "skip": "skip"}[verdict]
        proc = subprocess.run(
            ["git", "-C", str(self.repo.path), "-c", "core.hooksPath=/dev/null", "bisect", term, sha],
            capture_output=True, text=True, timeout=120, env=_GIT_ENV,
        )
        return self._status(proc.stdout + proc.stderr)

    def reset(self) -> None:
        self.repo.git("bisect", "reset", check=False)

    def remaining(self) -> int:
        bad, goods = self._terms()
        if not bad:
            return 0
        out = self.repo.git("rev-list", "--count", bad, *[f"^{g}" for g in goods], "--", *self.pathspec, check=False)
        return int(out.strip() or 0)

    def log(self) -> str:
        return self.repo.git("bisect", "log", check=False)

    def _status(self, out: str) -> BisectStatus:
        m = re.search(r"^([0-9a-f]{40}) is the first bad commit", out, re.M)
        if m:
            return BisectStatus(first_bad=m.group(1), raw=out)
        if "could be any of" in out or "only 'skip'ped commits left" in out:
            cands = re.findall(r"^([0-9a-f]{40})$", out, re.M)
            return BisectStatus(candidates=cands, raw=out)
        head = self.repo.git("rev-parse", "--verify", "--quiet", "BISECT_HEAD", check=False).strip()
        if not head:
            raise GitError(f"bisect produced no candidate: {out.strip()[-500:]}")
        return BisectStatus(next_sha=head, raw=out)

    # ---------------------------------------------------------- speculation
    def _terms(self) -> tuple[str | None, list[str]]:
        refs = self.repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/bisect/")
        bad, goods = None, []
        for line in refs.splitlines():
            name, sha = line.split()
            if name == "refs/bisect/bad":
                bad = sha
            elif name.startswith("refs/bisect/good-"):
                goods.append(sha)
        return bad, goods

    def _midpoint(self, bad: str, goods: list[str]) -> str | None:
        out = self.repo.git("rev-list", "--bisect", bad, *[f"^{g}" for g in goods], "--", *self.pathspec,
                            check=False).strip()
        return out or None

    def predict(self, current: str) -> list[str]:
        """The commits git would ask for next if ``current`` turns out good, or bad.
        Testing them speculatively in parallel roughly halves wall-clock time."""
        bad, goods = self._terms()
        if not bad:
            return []
        out = []
        for nxt in (self._midpoint(bad, goods + [current]), self._midpoint(current, goods)):
            if nxt and nxt != current and nxt != bad and nxt not in out:
                out.append(nxt)
        return out


def touches_dependencies(files: list[str]) -> list[str]:
    return [f for f in files if DEPENDENCY_FILES.search(f)]
