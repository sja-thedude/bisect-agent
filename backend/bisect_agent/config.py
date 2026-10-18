"""All tunables in one place. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Limits:
    # Hard limits from the spec. Hitting any of them ends the job with partial findings.
    max_bisect_steps: int = field(default_factory=lambda: _int("BISECT_MAX_STEPS", 15))
    test_timeout_s: int = field(default_factory=lambda: _int("BISECT_TEST_TIMEOUT_S", 300))
    job_timeout_s: int = field(default_factory=lambda: _int("BISECT_JOB_TIMEOUT_S", 25 * 60))
    default_range: int = field(default_factory=lambda: _int("BISECT_DEFAULT_RANGE", 200))
    install_timeout_s: int = field(default_factory=lambda: _int("BISECT_INSTALL_TIMEOUT_S", 300))
    # Speculative workers: while commit M runs we also test the two possible next midpoints.
    parallel_runs: int = field(default_factory=lambda: _int("BISECT_PARALLEL", 3))


@dataclass(frozen=True)
class SandboxConfig:
    image: str = field(default_factory=lambda: _env("BISECT_SANDBOX_IMAGE", "bisect-sandbox:py3.11"))
    deps_volume: str = field(default_factory=lambda: _env("BISECT_DEPS_VOLUME", "bisect-deps"))
    memory: str = field(default_factory=lambda: _env("BISECT_SANDBOX_MEMORY", "2g"))
    cpus: str = field(default_factory=lambda: _env("BISECT_SANDBOX_CPUS", "2"))
    pids: int = field(default_factory=lambda: _int("BISECT_SANDBOX_PIDS", 512))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("BISECT_DATA_DIR", str(Path.cwd() / "data"))).resolve())
    limits: Limits = field(default_factory=Limits)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bisect.sqlite3"


def settings() -> Settings:
    return Settings()
