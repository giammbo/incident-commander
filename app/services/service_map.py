from __future__ import annotations

from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Component, Incident, System


def _bfs(starts: set[int], rev: dict[int, list[int]]) -> set[int]:
    seen = set(starts)
    q = deque(starts)
    while q:
        n = q.popleft()
        for m in rev.get(n, []):
            if m not in seen:
                seen.add(m)
                q.append(m)
    return seen


def build_graph(db: Session) -> dict:
    systems = list(db.scalars(select(System)))
    components = list(db.scalars(select(Component)))
    sys_by_id = {s.id: s for s in systems}

    from app.models import StatusCategory, StatusLevel

    open_incidents = list(
        db.scalars(
            select(Incident)
            .outerjoin(StatusLevel, Incident.status_id == StatusLevel.id)
            .where((StatusLevel.id.is_(None)) | (StatusLevel.category != StatusCategory.closed))
        )
    )
    down_components: set[int] = set()
    down_systems: set[int] = set()
    for inc in open_incidents:
        comps = list(inc.components)
        if comps:
            down_components |= {c.id for c in comps}
        elif inc.system_id is not None:
            down_systems.add(inc.system_id)
    for sid in list(down_systems):
        s = sys_by_id.get(sid)
        if s is not None:
            down_components |= {c.id for c in s.components}

    # reverse dependency adjacency: down node -> nodes that depend on it
    comp_rev: dict[int, list[int]] = {}
    for c in components:
        for dep in c.depends_on:
            comp_rev.setdefault(dep.id, []).append(c.id)
    sys_rev: dict[int, list[int]] = {}
    for s in systems:
        for dep in s.depends_on:
            sys_rev.setdefault(dep.id, []).append(s.id)

    impacted_components = _bfs(down_components, comp_rev) - down_components
    impacted_systems = _bfs(down_systems, sys_rev) - down_systems

    def _comp_status(cid: int) -> str:
        if cid in down_components:
            return "down"
        return "impacted" if cid in impacted_components else "ok"

    def _sys_status(sid: int) -> str:
        if sid in down_systems:
            return "down"
        return "impacted" if sid in impacted_systems else "ok"

    nodes: list[dict] = []
    for s in systems:
        nodes.append(
            {"id": f"sys-{s.id}", "label": s.name, "type": "system", "status": _sys_status(s.id)}
        )
    for c in components:
        parent = sys_by_id.get(c.system_id)
        nodes.append(
            {
                "id": f"comp-{c.id}",
                "label": c.name,
                "type": "component",
                "system": parent.name if parent else None,
                "status": _comp_status(c.id),
            }
        )

    links: list[dict] = []
    for c in components:
        links.append({"source": f"sys-{c.system_id}", "target": f"comp-{c.id}", "kind": "contains"})
        for dep in c.depends_on:
            links.append({"source": f"comp-{c.id}", "target": f"comp-{dep.id}", "kind": "dep"})
    for s in systems:
        for dep in s.depends_on:
            links.append({"source": f"sys-{s.id}", "target": f"sys-{dep.id}", "kind": "dep"})

    return {"nodes": nodes, "links": links}
