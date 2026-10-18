"""Docker sandbox: the only place untrusted repository code is ever executed.

Every test run is a fresh container that:
  * has no network (``--network none``),
  * sees the commit under test as a read-only bind mount at /src,
  * sees its installed dependencies as a read-only volume at /deps,
  * works on a private tmpfs copy at /work that vanishes with the container,
  * runs as an unprivileged user with all capabilities dropped,
  * is killed hard at the timeout, from both inside (``timeout``) and outside (``docker kill``).

Dependencies are the one controlled path to the network. They are resolved in
two steps so that no repository code ever runs with network access:
  1. ``deps_spec`` (network none) reads the commit's metadata and prints requirement strings.
  2. ``install`` (network on, NO repository mounted) pip-installs those strings into a
     content-addressed directory on the deps volume, cached across commits and jobs.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings

RUNNER_UID = 10001
SANDBOX_DIR = Path(__file__).resolve().parents[2] / "sandbox"
SUPPORTED_PYTHONS = ("3.8", "3.9", "3.10", "3.11", "3.12", "3.13")
JUNIT_BEGIN = "@@BISECT_JUNIT_BEGIN@@"
JUNIT_END = "@@BISECT_JUNIT_END@@"


class SandboxError(RuntimeError):
    """Infrastructure failure (Docker unavailable, image missing), not a test verdict."""


@dataclass
class TestCase:
    nodeid: str
    outcome: str  # passed | failed | error | skipped
    message: str = ""


@dataclass
class TestRun:
    sha: str
    status: str  # passed | failed | error | timeout | no_tests | install_failed
    exit_code: int | None
    duration_s: float
    output_tail: str
    cases: list[TestCase] = field(default_factory=list)
    deps_key: str = ""
    requirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _docker(*args: str, timeout: float | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input)
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found") from e


class Sandbox:
    def __init__(self, settings: Settings, python: str | None = None):
        self.s = settings
        self.cfg = settings.sandbox
        self.image = self.cfg.image if not python else f"{self.cfg.image.rsplit(':', 1)[0]}:py{python}"
        self.python = python or self.cfg.image.rsplit(":py", 1)[-1]
        self.s.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._deps_locks: dict[str, threading.Lock] = {}
        self._deps_guard = threading.Lock()
        self._ready = False

    # ------------------------------------------------------------------ setup
    def ensure_ready(self) -> None:
        if self._ready:
            return
        if _docker("info", "--format", "{{.ServerVersion}}", timeout=30).returncode != 0:
            raise SandboxError("Docker daemon is not reachable")
        if _docker("image", "inspect", self.image, timeout=30).returncode != 0:
            build = subprocess.run(
                ["docker", "build", "-q", "-t", self.image, "--build-arg", f"PY={self.python}", str(SANDBOX_DIR)],
                capture_output=True, text=True, timeout=900,
            )
            if build.returncode != 0:
                raise SandboxError(f"could not build sandbox image {self.image}: {build.stderr[-500:]}")
        _docker("volume", "create", self.cfg.deps_volume, timeout=30)
        # A fresh named volume is root-owned; hand it to the unprivileged runner once.
        _docker("run", "--rm", "--network", "none", "-u", "0", "-v", f"{self.cfg.deps_volume}:/deps",
                self.image, "chown", str(RUNNER_UID), "/deps", timeout=60)
        self._ready = True

    # -------------------------------------------------------------- snapshots
    def snapshot(self, repo: Path, sha: str, key: str | None = None) -> Path:
        """Materialise a commit as plain files (no .git) for a read-only mount.

        Uses a throwaway index + checkout-index rather than ``git archive`` so that
        ``export-ignore`` attributes cannot hide the test suite.
        """
        dest = self.s.snapshots_dir / (key or repo.name) / sha
        if (dest / ".bisect-complete").exists():
            return dest
        tmp = dest.with_name(sha + ".tmp-" + uuid.uuid4().hex[:6])
        tmp.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
            base = ["git", f"--git-dir={repo}", f"--work-tree={tmp}", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.symlinks=true", "-c", "core.bare=false"]
            for step in (["read-tree", sha], ["checkout-index", "-a", "-f"]):
                proc = subprocess.run([*base, *step], env=env, capture_output=True, text=True)
                if proc.returncode != 0:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise SandboxError(f"snapshot of {sha[:10]} failed: {proc.stderr.strip()[-300:]}")
        (tmp / ".bisect-complete").write_text(sha)
        os.chmod(tmp, 0o755)
        if dest.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            tmp.rename(dest)
        return dest

    # ------------------------------------------------------------ dependencies
    def _base_run(self, name: str, network: str) -> list[str]:
        return [
            "run", "--rm", "--name", name,
            "--network", network,
            "--user", str(RUNNER_UID),
            "--read-only",
            "--tmpfs", f"/work:rw,exec,size=1g,uid={RUNNER_UID}",
            "--tmpfs", f"/tmp:rw,exec,size=512m,uid={RUNNER_UID}",
            "--tmpfs", f"/home/runner:rw,size=64m,uid={RUNNER_UID}",
            "--memory", self.cfg.memory, "--cpus", self.cfg.cpus,
            "--pids-limit", str(self.cfg.pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        ]

    def _run_container(self, args: list[str], name: str, timeout: float) -> tuple[int | None, str, bool]:
        """Run a container; returns (exit_code, combined output, timed_out)."""
        try:
            proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout + 15)
            return proc.returncode, proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as e:
            _docker("kill", name, timeout=30)
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, out, True

    def requirements_for(self, snapshot: Path) -> tuple[list[str], list[str]]:
        """(requirements, lock-file constraints) for a snapshot, read with no network."""
        name = f"bisect-meta-{uuid.uuid4().hex[:10]}"
        args = self._base_run(name, "none") + [
            "-v", f"{snapshot}:/src:ro",
            self.image, "sh", "-c", "cp -a /src/. /work/ && python /opt/bisect/deps_spec.py",
        ]
        code, out, timed_out = self._run_container(args, name, 180)
        for line in reversed(out.strip().splitlines()):
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    return data["requirements"], data.get("constraints", [])
                except (ValueError, KeyError):
                    break
        return [], []

    def install(self, requirements: list[str], constraints: list[str] | None = None) -> tuple[str, bool, str]:
        """Install requirement strings into /deps/<key>. Returns (key, ok, log tail).

        The repository is NOT mounted here, so the only code that runs with network
        access is pip and the third-party packages it builds.
        """
        reqs = sorted(set(requirements))
        cons = sorted(set(constraints or []))
        key = hashlib.sha256(("\n".join(reqs) + "|" + "\n".join(cons) + self.image).encode()).hexdigest()[:16]
        with self._deps_guard:
            lock = self._deps_locks.setdefault(key, threading.Lock())
        with lock:
            probe = _docker("run", "--rm", "--network", "none", "-v", f"{self.cfg.deps_volume}:/deps:ro",
                            self.image, "test", "-f", f"/deps/{key}/.ok", timeout=60)
            if probe.returncode == 0:
                return key, True, "cached"
            if not reqs:
                cmd = f"mkdir -p /deps/{key} && touch /deps/{key}/.ok"
            else:
                quoted = " ".join("'" + r.replace("'", "") + "'" for r in reqs)
                cons_file = "\n".join(c.replace("'", "") for c in cons)
                cmd = (f"printf '%s\\n' '{cons_file}' > /tmp/constraints.txt && rm -rf /deps/{key} && "
                       f"pip install --quiet --no-input --target /deps/{key} -c /tmp/constraints.txt {quoted} "
                       f"&& touch /deps/{key}/.ok")
            name = f"bisect-install-{uuid.uuid4().hex[:10]}"
            args = self._base_run(name, "bridge") + [
                "-v", f"{self.cfg.deps_volume}:/deps",
                self.image, "sh", "-c", cmd,
            ]
            code, out, timed_out = self._run_container(args, name, self.s.limits.install_timeout_s)
            ok = code == 0 and not timed_out
            return key, ok, out[-3000:]

    # ------------------------------------------------------------------ tests
    def run_pytest(
        self,
        snapshot: Path,
        sha: str,
        deps_key: str,
        inject: dict[str, str] | None = None,
        targets: list[str] | None = None,
        timeout_s: int | None = None,
    ) -> TestRun:
        """Run pytest against a snapshot with no network. ``inject`` maps repo-relative
        paths to file contents (the reproduction test) that are dropped into /work."""
        timeout_s = timeout_s or self.s.limits.test_timeout_s
        name = f"bisect-test-{uuid.uuid4().hex[:10]}"
        inject = inject or {}
        with tempfile.TemporaryDirectory(dir=self.s.snapshots_dir) as inj_dir:
            os.chmod(inj_dir, 0o755)
            copy_cmds = []
            for i, (rel, content) in enumerate(inject.items()):
                rel = os.path.normpath(rel).lstrip("/")
                if rel.startswith(".."):
                    raise ValueError(f"refusing to inject outside the repo: {rel}")
                p = Path(inj_dir) / f"f{i}"
                p.write_text(content)
                os.chmod(p, 0o644)
                copy_cmds.append(f"mkdir -p \"$(dirname '/work/{rel}')\" && cp /inject/f{i} '/work/{rel}'")
            target_args = " ".join("'" + t.replace("'", "") + "'" for t in (targets or []))
            setup = " && ".join([
                "cp -a /src/. /work/",
                *copy_cmds,
                f"export PYTHONPATH=/work/src:/work:/deps/{deps_key}",
                f"export PATH=/deps/{deps_key}/bin:$PATH",
            ])
            script = (
                f"{setup} || exit 3\n"
                f"timeout -s KILL {timeout_s} python -m pytest -p no:cacheprovider -o addopts= -q -rA "
                f"--junitxml=/tmp/junit.xml {target_args} > /tmp/out.txt 2>&1\n"
                "rc=$?\n"
                "tail -c 20000 /tmp/out.txt\n"
                f"echo; echo {JUNIT_BEGIN}; cat /tmp/junit.xml 2>/dev/null; echo; echo {JUNIT_END}\n"
                "exit $rc\n"
            )
            args = self._base_run(name, "none") + [
                "-v", f"{snapshot}:/src:ro",
                "-v", f"{self.cfg.deps_volume}:/deps:ro",
                "-v", f"{inj_dir}:/inject:ro",
                "-e", "PYTEST_ADDOPTS=",
                self.image, "bash", "-c", script,
            ]
            t0 = time.monotonic()
            code, out, timed_out = self._run_container(args, name, timeout_s)
            duration = time.monotonic() - t0
        text, cases = _split_junit(out)
        if timed_out or code == 137:
            status = "timeout"
        elif code == 0:
            status = "passed"
        elif code == 1:
            status = "failed"
        elif code == 5:
            status = "no_tests"
        else:
            status = "error"
        # Collection errors come back as exit 2 with an "error" case; keep them distinct from failures.
        return TestRun(sha=sha, status=status, exit_code=code, duration_s=round(duration, 2),
                       output_tail=text[-6000:], cases=cases, deps_key=deps_key)


def _split_junit(out: str) -> tuple[str, list[TestCase]]:
    if JUNIT_BEGIN not in out:
        return out, []
    text, rest = out.split(JUNIT_BEGIN, 1)
    xml = rest.split(JUNIT_END, 1)[0].strip()
    cases: list[TestCase] = []
    if xml:
        try:
            root = ET.fromstring(xml)
            for tc in root.iter("testcase"):
                nodeid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
                outcome, msg = "passed", ""
                for tag in ("failure", "error", "skipped"):
                    el = tc.find(tag)
                    if el is not None:
                        outcome = {"failure": "failed"}.get(tag, tag)
                        msg = (el.get("message") or "")[:500]
                        break
                cases.append(TestCase(nodeid, outcome, msg))
        except ET.ParseError:
            pass
    return text.rstrip(), cases
