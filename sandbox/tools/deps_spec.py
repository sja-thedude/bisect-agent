"""Runs INSIDE the sandbox, with no network.

Reads a checked-out commit at /work and prints a JSON list of requirement
strings needed to import and test it. Never installs anything.
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement

TEST_EXTRAS = {"test", "tests", "testing", "dev", "develop"}
ROOT = "/work"


def from_metadata():
    """Ask pip for the project's metadata without touching the network."""
    if not any(os.path.exists(os.path.join(ROOT, f)) for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        return None
    with tempfile.TemporaryDirectory() as d:
        report = os.path.join(d, "report.json")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--no-index", "--no-deps",
             "--no-build-isolation", "--quiet", "--report", report, ROOT],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not os.path.exists(report):
            sys.stderr.write(proc.stderr[-2000:])
            return None
        with open(report) as f:
            data = json.load(f)
    reqs = []
    for item in data.get("install", []):
        meta = item.get("metadata", {})
        extras = set(meta.get("provides_extra", [])) & TEST_EXTRAS
        for raw in meta.get("requires_dist", []) or []:
            reqs.append((raw, extras))
    return reqs


def from_requirement_files():
    out = []
    patterns = ["requirements*.txt", "requirements/*.txt", "*requirements*.txt", "test-requirements.txt"]
    seen = set()
    for pat in patterns:
        for path in sorted(glob.glob(os.path.join(ROOT, pat))):
            if path in seen or "doc" in os.path.basename(path).lower():
                continue
            seen.add(path)
            for line in open(path, errors="replace"):
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith(("-", "git+", "http")) or "://" in line:
                    continue
                out.append((line, set()))
    return out


def resolve(pairs):
    env = default_environment()
    result, names = [], set()
    for raw, extras in pairs:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if req.marker is not None:
            ok = req.marker.evaluate({**env, "extra": ""}) or any(
                req.marker.evaluate({**env, "extra": e}) for e in extras
            )
            if not ok:
                continue
        req.marker = None
        key = re.sub(r"[-_.]+", "-", req.name).lower()
        if key in {"pytest"} or key in names:
            continue
        names.add(key)
        result.append(str(req))
    return result


def main():
    pairs = from_metadata()
    source = "metadata"
    if pairs is None:
        pairs, source = [], "none"
    files = from_requirement_files()
    if files:
        pairs += files
        source += "+requirements"
    print(json.dumps({"source": source, "requirements": resolve(pairs)}))


if __name__ == "__main__":
    main()
