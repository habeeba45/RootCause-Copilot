"""SQLite-backed incident store.

Database: <RCA_DATA_DIR>/rootcause.db (default backend/data/rootcause.db), WAL mode.

Tables
    incidents    one row per incident (metadata, status, validation report, pointer to latest run)
    input_files  the uploaded/sample files, stored as BLOBs so an incident can be re-analysed any time
    runs         one row per analysis run - full result JSON + summary; re-running keeps earlier runs (history)
    events       the agent activity log of each run, written as it happens (survives a crash mid-run)

Incidents stored by the earlier JSON-file version (data/incidents/*/incident.json) are imported automatically.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RCA_DATA_DIR", BASE / "data"))
DB_PATH = DATA_DIR / "rootcause.db"
LEGACY_DIR = DATA_DIR / "incidents"
SAMPLE_DIR = BASE / "sample_data"

INPUT_KINDS = ("metrics", "logs", "changes", "dependencies")
SAMPLE_FILES = {"metrics": "metrics.csv", "logs": "app.log", "changes": "changes.json", "dependencies": "dependencies.json"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, severity TEXT, source TEXT, sample_id TEXT,
    created_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT, analyzed_at TEXT,
    files_json TEXT NOT NULL DEFAULT '{}', validation_json TEXT, current_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS input_files (
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY (incident_id, kind)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    number INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    mode TEXT, error TEXT, summary_json TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_runs_incident ON runs(incident_id, number);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _j(value) -> str | None:
    return None if value is None else json.dumps(value)


class Store:
    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
            self._recover_interrupted()
        self.migrated = self._import_legacy()

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, args)

    def _recover_interrupted(self):
        """Runs that were in progress when the server stopped can never finish: mark them failed."""
        self.db.execute("UPDATE runs SET status='failed', error='Analysis interrupted by server restart', finished_at=? "
                        "WHERE status='running'", (now_iso(),))
        self.db.execute("UPDATE incidents SET status='failed', error='Analysis interrupted by server restart' "
                        "WHERE status='analyzing'")

    def _row_to_incident(self, row: sqlite3.Row, full: bool) -> dict:
        inc = {k: row[k] for k in ("id", "title", "description", "severity", "source", "sample_id", "created_at",
                                    "status", "error", "analyzed_at")}
        inc["files"] = json.loads(row["files_json"] or "{}")
        inc["validation"] = json.loads(row["validation_json"]) if row["validation_json"] else None
        inc["current_run_id"] = row["current_run_id"]
        inc["result"] = None
        inc["run_count"] = self._q("SELECT COUNT(*) FROM runs WHERE incident_id=?", (row["id"],))[0][0]
        if row["current_run_id"]:
            r = self._q("SELECT number, summary_json, result_json, status FROM runs WHERE id=?", (row["current_run_id"],))
            if r and r[0]["status"] == "completed":
                if full and r[0]["result_json"]:
                    inc["result"] = json.loads(r[0]["result_json"])
                elif r[0]["summary_json"]:
                    inc["result"] = {"summary": json.loads(r[0]["summary_json"])}
            if r:
                inc["run_number"] = r[0]["number"]
        return inc

    # ------------------------------------------------------------------ incidents
    def create(self, title: str, description: str, severity: str, source: str, files: dict[str, tuple[str, bytes]],
               sample_id: str | None = None) -> dict:
        iid = "INC-" + uuid.uuid4().hex[:6].upper()
        meta = {kind: {"name": name, "bytes": len(data)} for kind, (name, data) in files.items()}
        with self._lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("INSERT INTO incidents (id, title, description, severity, source, sample_id, created_at, status, files_json) "
                                "VALUES (?,?,?,?,?,?,?,?,?)",
                                (iid, title, description, severity, source, sample_id, now_iso(), "uploaded", json.dumps(meta)))
                for kind, (name, data) in files.items():
                    self.db.execute("INSERT INTO input_files VALUES (?,?,?,?)", (iid, kind, name, sqlite3.Binary(data)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.get(iid)

    def get(self, iid: str) -> dict | None:
        rows = self._q("SELECT * FROM incidents WHERE id=?", (iid,))
        return self._row_to_incident(rows[0], full=True) if rows else None

    def list(self) -> list[dict]:
        return [self._row_to_incident(r, full=False) for r in self._q("SELECT * FROM incidents ORDER BY created_at DESC, rowid DESC")]

    def count(self) -> int:
        return self._q("SELECT COUNT(*) FROM incidents")[0][0]

    def delete(self, iid: str) -> bool:
        return self._x("DELETE FROM incidents WHERE id=?", (iid,)).rowcount > 0

    def read_input(self, iid: str, kind: str) -> bytes | None:
        rows = self._q("SELECT data FROM input_files WHERE incident_id=? AND kind=?", (iid, kind))
        return bytes(rows[0]["data"]) if rows else None

    def update(self, iid: str, **fields) -> None:
        cols = {"status": "status", "error": "error", "analyzed_at": "analyzed_at", "title": "title"}
        sets, args = [], []
        for k, v in fields.items():
            if k in cols:
                sets.append(f"{cols[k]}=?")
                args.append(v)
            elif k == "files":
                sets.append("files_json=?")
                args.append(json.dumps(v))
            elif k == "validation":
                sets.append("validation_json=?")
                args.append(_j(v))
            else:
                raise KeyError(f"Unknown incident field {k}")
        if sets:
            self._x(f"UPDATE incidents SET {', '.join(sets)} WHERE id=?", (*args, iid))

    # ------------------------------------------------------------------ runs (investigation history)
    def start_run(self, iid: str, mode: str | None) -> int:
        with self._lock:
            n = self.db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM runs WHERE incident_id=?", (iid,)).fetchone()[0]
            run_id = self.db.execute("INSERT INTO runs (incident_id, number, started_at, status, mode) VALUES (?,?,?,?,?)",
                                     (iid, n, now_iso(), "running", mode)).lastrowid
            self.db.execute("UPDATE incidents SET current_run_id=?, status='analyzing', error=NULL WHERE id=?", (run_id, iid))
        return run_id

    def finish_run(self, iid: str, run_id: int, status: str, result: dict | None = None, error: str | None = None) -> None:
        summary = (result or {}).get("summary")
        with self._lock:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE runs SET status=?, finished_at=?, error=?, summary_json=?, result_json=?, mode=COALESCE(?, mode) "
                            "WHERE id=?", (status, now_iso(), error, _j(summary), _j(result),
                                           (summary or {}).get("analysis_mode"), run_id))
            self.db.execute("UPDATE incidents SET status=?, error=?, analyzed_at=? WHERE id=?",
                            (status, error, now_iso() if status == "completed" else None, iid))
            self.db.execute("COMMIT")

    def list_runs(self, iid: str) -> list[dict]:
        out = []
        for r in self._q("SELECT id, number, started_at, finished_at, status, mode, error, summary_json, "
                         "(SELECT COUNT(*) FROM events e WHERE e.run_id = runs.id) AS events "
                         "FROM runs WHERE incident_id=? ORDER BY number DESC", (iid,)):
            s = json.loads(r["summary_json"]) if r["summary_json"] else {}
            out.append({"id": r["id"], "number": r["number"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                        "status": r["status"], "mode": r["mode"], "error": r["error"], "events": r["events"],
                        "title": s.get("title"), "verdict": s.get("verdict"), "confidence": s.get("confidence"),
                        "service": s.get("service"), "duration_s": s.get("duration_s")})
        return out

    def get_run(self, iid: str, run_id: int) -> dict | None:
        rows = self._q("SELECT * FROM runs WHERE id=? AND incident_id=?", (run_id, iid))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "number": r["number"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                "status": r["status"], "mode": r["mode"], "error": r["error"],
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "events": self.get_run_events(run_id)}

    # ------------------------------------------------------------------ events
    def add_event(self, iid: str, event: dict) -> None:
        with self._lock:
            row = self.db.execute("SELECT current_run_id FROM incidents WHERE id=?", (iid,)).fetchone()
            if not row or not row[0]:
                return
            run_id = row[0]
            seq = self.db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            event["seq"] = seq
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (run_id, seq, json.dumps(event, default=str)))

    def get_run_events(self, run_id: int, after: int = -1) -> list[dict]:
        return [json.loads(r["json"]) for r in self._q("SELECT json FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after))]

    def get_events(self, iid: str, after: int = -1) -> list[dict]:
        rows = self._q("SELECT current_run_id FROM incidents WHERE id=?", (iid,))
        if not rows or not rows[0][0]:
            return []
        return self.get_run_events(rows[0][0], after)

    # ------------------------------------------------------------------ legacy import
    def _import_legacy(self) -> int:
        """Import incidents saved by the JSON-file version of the app, then rename the old folder."""
        if not LEGACY_DIR.exists() or self.path.parent != DATA_DIR:
            return 0
        n = 0
        for f in sorted(LEGACY_DIR.glob("*/incident.json")):
            try:
                inc = json.loads(f.read_text(encoding="utf-8"))
                if self._q("SELECT 1 FROM incidents WHERE id=?", (inc["id"],)):
                    continue
                files = {}
                for kind, meta in (inc.get("files") or {}).items():
                    p = f.parent / meta.get("stored_as", "")
                    if p.is_file():
                        files[kind] = (meta.get("name", p.name), p.read_bytes())
                with self._lock:
                    self.db.execute("BEGIN")
                    self.db.execute("INSERT INTO incidents (id, title, description, severity, source, sample_id, created_at, status, "
                                    "error, analyzed_at, files_json, validation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (inc["id"], inc.get("title", inc["id"]), inc.get("description"), inc.get("severity"),
                                     inc.get("source"), inc.get("sample_id"), inc.get("created_at") or now_iso(),
                                     "completed" if inc.get("result") else ("failed" if inc.get("status") == "analyzing" else inc.get("status", "uploaded")),
                                     inc.get("error"), inc.get("analyzed_at"),
                                     json.dumps({k: {"name": v[0], "bytes": len(v[1])} for k, v in files.items()}), _j(inc.get("validation"))))
                    for kind, (name, data) in files.items():
                        self.db.execute("INSERT INTO input_files VALUES (?,?,?,?)", (inc["id"], kind, name, sqlite3.Binary(data)))
                    if inc.get("result"):
                        run_id = self.db.execute("INSERT INTO runs (incident_id, number, started_at, finished_at, status, mode, "
                                                 "summary_json, result_json) VALUES (?,?,?,?,?,?,?,?)",
                                                 (inc["id"], 1, inc.get("created_at") or now_iso(), inc.get("analyzed_at"), "completed",
                                                  inc["result"].get("summary", {}).get("analysis_mode"),
                                                  _j(inc["result"].get("summary")), _j(inc["result"]))).lastrowid
                        ev_file = f.parent / "events.json"
                        events = json.loads(ev_file.read_text(encoding="utf-8")) if ev_file.exists() else []
                        for i, ev in enumerate(events):
                            self.db.execute("INSERT INTO events VALUES (?,?,?)", (run_id, i, json.dumps(ev)))
                        self.db.execute("UPDATE incidents SET current_run_id=? WHERE id=?", (run_id, inc["id"]))
                    self.db.execute("COMMIT")
                n += 1
            except Exception:  # noqa: BLE001 - a corrupt legacy record must not stop the server
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        if n:
            LEGACY_DIR.rename(LEGACY_DIR.with_name("incidents.migrated-to-sqlite"))
        return n


def list_samples() -> list[dict]:
    out = []
    for d in sorted(SAMPLE_DIR.iterdir()) if SAMPLE_DIR.exists() else []:
        meta = d / "meta.json"
        if meta.exists():
            m = json.loads(meta.read_text(encoding="utf-8"))
            m["files"] = {k: {"name": v, "bytes": (d / v).stat().st_size} for k, v in SAMPLE_FILES.items() if (d / v).exists()}
            out.append(m)
    return out
