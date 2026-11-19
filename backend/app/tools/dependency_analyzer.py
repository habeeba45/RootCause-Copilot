"""Dependency analysis tool: localises the fault in the service graph and computes blast radius."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from ..parsers import DependencyMap, iso

TOLERANCE_S = 90  # onset ordering tolerance between caller and callee


class DependencyAnalyzer:
    name = "dependency_analyzer"
    description = "Walks the service dependency graph to find the deepest affected services and cascade paths."

    def run(self, dep_map: DependencyMap, affected: dict[str, datetime], all_services: set[str] | None = None) -> dict:
        services = set(dep_map.services) | set(affected) | set(all_services or set())
        callees: dict[str, set[str]] = defaultdict(set)
        callers: dict[str, set[str]] = defaultdict(set)
        for a, b in dep_map.edges:
            callees[a].add(b)
            callers[b].add(a)

        def upstream_closure(s: str) -> set[str]:
            seen, q = set(), deque([s])
            while q:
                x = q.popleft()
                for c in callers.get(x, ()):
                    if c not in seen:
                        seen.add(c)
                        q.append(c)
            return seen

        nodes, roots = [], []
        for s in sorted(services):
            onset = affected.get(s)
            affected_callees = [c for c in callees.get(s, ()) if c in affected]
            if onset is None:
                status = "healthy"
            elif not affected_callees:
                status = "root_candidate"
            else:
                earlier = [c for c in affected_callees if (affected[c] - onset).total_seconds() <= TOLERANCE_S]
                status = "impacted" if earlier else "root_candidate"
            depth = self._depth(s, callees)
            info = dep_map.services.get(s, {})
            node = {
                "id": s, "status": status, "onset": iso(onset), "tier": info.get("tier"), "owner": info.get("owner"),
                "depth": depth, "callers": sorted(callers.get(s, ())), "callees": sorted(callees.get(s, ())),
                "in_map": s in dep_map.services,
            }
            nodes.append(node)
            if status == "root_candidate":
                blast = upstream_closure(s)
                affected_blast = sorted(b for b in blast if b in affected)
                coverage = (len(affected_blast) + 1) / max(1, len(affected))
                roots.append({"service": s, "onset": iso(onset), "onset_dt": onset,
                              "blast_radius": sorted(blast), "affected_upstream": affected_blast,
                              "coverage": round(coverage, 2)})
        roots.sort(key=lambda r: (-r["coverage"], r["onset_dt"]))

        edges = []
        for a, b in dep_map.edges:
            prop = a in affected and b in affected and (affected[a] - affected[b]).total_seconds() >= -TOLERANCE_S
            edges.append({"from": a, "to": b, "propagation": prop})

        # longest propagation chain from each root
        paths = []
        for r in roots:
            path = self._propagation_path(r["service"], callers, affected)
            if len(path) > 1:
                paths.append(path)

        return {
            "nodes": nodes,
            "edges": edges,
            "root_candidates": roots,
            "propagation_paths": paths,
            "unmapped_services": sorted(s for s in services if s not in dep_map.services),
            "has_map": bool(dep_map.edges),
            "upstream_of": {s: sorted(upstream_closure(s)) for s in services},
        }

    @staticmethod
    def _depth(s: str, callees: dict[str, set[str]]) -> int:
        seen = set()

        def d(x: str) -> int:
            if x in seen:
                return 0
            seen.add(x)
            ch = callees.get(x, ())
            r = 0 if not ch else 1 + max(d(c) for c in ch)
            seen.discard(x)
            return r
        return d(s)

    @staticmethod
    def _propagation_path(root: str, callers: dict[str, set[str]], affected: dict[str, datetime]) -> list[str]:
        best = [root]

        def walk(node: str, path: list[str]):
            nonlocal best
            if len(path) > len(best):
                best = list(path)
            for c in sorted(callers.get(node, ())):
                if c in affected and c not in path:
                    walk(c, path + [c])
        walk(root, [root])
        return best
