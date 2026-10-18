"""Tavily search, used when the culprit commit is a dependency bump.

The repo's own history can't explain a break that happened upstream, so the agent
searches the package's changelog, release notes, issue tracker and CVE databases.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field

import httpx

TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class SearchHit:
    title: str
    url: str
    content: str
    score: float


@dataclass
class SearchRecord:
    purpose: str
    query: str
    latency_s: float
    hits: list[SearchHit] = field(default_factory=list)
    answer: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Tavily:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._client = httpx.Client(timeout=60, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, purpose: str, *, include_domains: list[str] | None = None,
               max_results: int = 5, depth: str = "advanced") -> SearchRecord:
        body = {"query": query, "search_depth": depth, "max_results": max_results, "include_answer": True}
        if include_domains:
            body["include_domains"] = include_domains
        t0 = time.monotonic()
        try:
            r = self._client.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return SearchRecord(purpose, query, round(time.monotonic() - t0, 3), error=str(e)[:300])
        hits = [SearchHit(h.get("title", ""), h.get("url", ""), (h.get("content") or "")[:1500], float(h.get("score") or 0))
                for h in data.get("results", [])]
        return SearchRecord(purpose, query, round(time.monotonic() - t0, 3), hits, data.get("answer") or "")


# ------------------------------------------------------------------ bump parsing
_REQ_LINE = re.compile(r"^\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*([<>=!~]=?[^;'\",#\s]+(?:\s*,\s*[<>=!~]=?[^;'\",#\s]+)*)?")


def parse_dependency_changes(diff: str) -> list[dict]:
    """Pull (package, old spec, new spec) pairs out of a unified diff of dependency files.

    Works for requirements*.txt lines and the quoted strings in pyproject/setup.py lists.
    Lock files are ignored here; the REASON tier reads those diffs directly.
    """
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith(("---", "+++")) or not line[:1] in "+-":
            continue
        if current_file.endswith((".lock",)) or "lock" in current_file.rsplit("/", 1)[-1]:
            continue
        body = line[1:].strip().rstrip(",")
        m = _REQ_LINE.match(body)
        if not m or not m.group(3):
            continue
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if name in {"python", "requires-python", "version"}:
            continue
        (removed if line[0] == "-" else added)[name] = m.group(3).replace(" ", "")
    changes = []
    for name in sorted(set(removed) | set(added)):
        old, new = removed.get(name), added.get(name)
        if old != new:
            changes.append({"package": name, "old": old, "new": new})
    return changes
