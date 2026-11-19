"""Evidence ledger: every fact a tool observes gets an ID (E1, E2, …).

Hypothesis checks, the narrative, recommendations and any LLM statement must cite these IDs,
so every conclusion can be traced back to the data it came from.
"""
from __future__ import annotations

import re
from datetime import datetime

from .parsers import iso

CITATION_RE = re.compile(r"\[?\b(E\d{1,4})\b\]?")


class EvidenceLedger:
    def __init__(self):
        self.items: list[dict] = []
        self._by_id: dict[str, dict] = {}

    def add(self, kind: str, tool: str, summary: str, service: str | None = None,
            time: datetime | str | None = None, data: dict | None = None) -> str:
        eid = f"E{len(self.items) + 1}"
        item = {"id": eid, "kind": kind, "tool": tool, "service": service,
                "time": iso(time) if isinstance(time, datetime) else time, "summary": summary, "data": data or {}}
        self.items.append(item)
        self._by_id[eid] = item
        return eid

    def get(self, eid: str) -> dict | None:
        return self._by_id.get(eid)

    def exists(self, eid: str) -> bool:
        return eid in self._by_id

    def valid(self, ids) -> list[str]:
        out = []
        for i in ids or []:
            i = str(i).strip().strip("[]")
            if i in self._by_id and i not in out:
                out.append(i)
        return out

    def by(self, kind: str | None = None, service: str | None = None) -> list[dict]:
        return [e for e in self.items if (kind is None or e["kind"] == kind) and (service is None or e["service"] == service)]

    def compact(self, limit: int = 150) -> list[dict]:
        """Short form for LLM context."""
        return [{"id": e["id"], "kind": e["kind"], "service": e["service"], "time": e["time"], "summary": e["summary"]}
                for e in self.items[:limit]]

    def ground_text(self, text: str) -> tuple[str, list[str], list[str]]:
        """Return (text with unknown citations removed, valid ids cited, invalid ids removed)."""
        valid, invalid = [], []

        def repl(m):
            eid = m.group(1)
            if self.exists(eid):
                if eid not in valid:
                    valid.append(eid)
                return f"[{eid}]"
            invalid.append(eid)
            return ""
        cleaned = CITATION_RE.sub(repl, text or "")
        cleaned = re.sub(r"\s+([.,;:)])", r"\1", re.sub(r"\s{2,}", " ", cleaned))
        return cleaned.strip(), valid, invalid
