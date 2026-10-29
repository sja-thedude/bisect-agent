"""SQLite persistence for runs and their event streams, so finished runs have permalinks."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  finished_at REAL,
  repo_url TEXT NOT NULL,
  bug_report TEXT NOT NULL,
  phase TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '',
  seed INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def create_run(self, state: dict, seed: bool = False, title: str = "") -> None:
        inp = state["input"]
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO runs (id, created_at, finished_at, repo_url, bug_report, phase, outcome, seed, title, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (state["id"], state["created_at"], state.get("finished_at") or None, inp["repo_url"], inp["bug_report"],
                 state["phase"], state.get("outcome", ""), int(seed), title, json.dumps(state)),
            )

    def save_state(self, state: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE runs SET phase=?, outcome=?, finished_at=?, state=? WHERE id=?",
                (state["phase"], state.get("outcome", ""), state.get("finished_at") or None, json.dumps(state), state["id"]),
            )

    def add_event(self, run_id: str, kind: str, payload: dict, ts: float | None = None) -> int:
        with self._lock:
            if run_id not in self._seq:
                row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id=?", (run_id,)).fetchone()
                self._seq[run_id] = row[0]
            self._seq[run_id] += 1
            seq = self._seq[run_id]
            self._db.execute("INSERT INTO events (run_id, seq, ts, kind, payload) VALUES (?,?,?,?,?)",
                             (run_id, seq, ts or time.time(), kind, json.dumps(payload, default=str)))
            return seq

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        rows = self._db.execute("SELECT seq, ts, kind, payload FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                                (run_id, after)).fetchall()
        return [{"seq": s, "ts": t, "kind": k, "payload": json.loads(p)} for s, t, k, p in rows]

    def get(self, run_id: str) -> dict | None:
        row = self._db.execute("SELECT state, seed, title FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        state = json.loads(row[0])
        state["seed"] = bool(row[1])
        state["title"] = row[2]
        return state

    def list(self, limit: int = 30) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, created_at, finished_at, repo_url, bug_report, phase, outcome, seed, title, "
            "json_extract(state, '$.first_bad.sha'), json_extract(state, '$.first_bad.subject') "
            "FROM runs ORDER BY seed DESC, created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ["id", "created_at", "finished_at", "repo_url", "bug_report", "phase", "outcome", "seed", "title",
                "first_bad_sha", "first_bad_subject"]
        return [dict(zip(keys, r)) for r in rows]

    def has(self, run_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is not None

    def mark_interrupted(self) -> None:
        """Runs that were in flight when the server stopped can never finish."""
        with self._lock:
            rows = self._db.execute("SELECT id, state FROM runs WHERE phase != 'done'").fetchall()
            for rid, raw in rows:
                st = json.loads(raw)
                st.update(phase="done", outcome="failed", error="server restarted while the run was in progress",
                          finished_at=time.time())
                self._db.execute("UPDATE runs SET phase='done', outcome='failed', finished_at=?, state=? WHERE id=?",
                                 (st["finished_at"], json.dumps(st), rid))

    def export(self, run_id: str) -> dict:
        return {"state": self.get(run_id), "events": self.events(run_id)}

    def import_run(self, data: dict, title: str = "") -> None:
        state = data["state"]
        state.pop("seed", None)
        state.pop("title", None)
        self.create_run(state, seed=True, title=title or state.get("title", ""))
        self.save_state(state)
        with self._lock:
            self._db.execute("DELETE FROM events WHERE run_id=?", (state["id"],))  # no leftovers from an older run
        for ev in data["events"]:
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO events (run_id, seq, ts, kind, payload) VALUES (?,?,?,?,?)",
                                 (state["id"], ev["seq"], ev["ts"], ev["kind"], json.dumps(ev["payload"])))
        self._seq.pop(state["id"], None)
