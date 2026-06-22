import sqlalchemy

from app.models import Component, Incident, SeverityLevel, StatusCategory, StatusLevel, System
from app.services import service_map, statuses


def _sev(db):
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    return lvl


def _status(graph, node_id):
    return next(n["status"] for n in graph["nodes"] if n["id"] == node_id)


def _seed_statuses(db):
    statuses.seed_status_levels(db)
    db.flush()
    active = next(
        s for s in db.scalars(sqlalchemy.select(StatusLevel)) if s.category == StatusCategory.active
    )
    closed = next(
        s for s in db.scalars(sqlalchemy.select(StatusLevel)) if s.category == StatusCategory.closed
    )
    return active, closed


def test_graph_nodes_links_and_ok_by_default(db_session):
    s = System(name="Billing")
    db_session.add(s)
    db_session.flush()
    a = Component(name="A", system_id=s.id)
    b = Component(name="B", system_id=s.id)
    db_session.add_all([a, b])
    db_session.flush()
    a.depends_on = [b]
    db_session.flush()
    g = service_map.build_graph(db_session)
    ids = {n["id"] for n in g["nodes"]}
    assert {f"sys-{s.id}", f"comp-{a.id}", f"comp-{b.id}"} <= ids
    kinds = {(lnk["source"], lnk["target"], lnk["kind"]) for lnk in g["links"]}
    assert (f"sys-{s.id}", f"comp-{a.id}", "contains") in kinds
    assert (f"comp-{a.id}", f"comp-{b.id}", "dep") in kinds
    assert all(n["status"] == "ok" for n in g["nodes"])  # no incidents


def test_component_incident_downs_it_and_impacts_dependents(db_session):
    lvl = _sev(db_session)
    active, _closed = _seed_statuses(db_session)
    s = System(name="Billing")
    db_session.add(s)
    db_session.flush()
    a = Component(name="A", system_id=s.id)  # A depends on B
    b = Component(name="B", system_id=s.id)
    db_session.add_all([a, b])
    db_session.flush()
    a.depends_on = [b]
    inc = Incident(title="B down", severity_level_id=lvl.id, status_id=active.id, system_id=s.id)
    inc.components = [b]
    db_session.add(inc)
    db_session.flush()
    g = service_map.build_graph(db_session)
    assert _status(g, f"comp-{b.id}") == "down"  # B has the incident
    assert _status(g, f"comp-{a.id}") == "impacted"  # A depends on B → blast radius
    assert _status(g, f"sys-{s.id}") == "ok"  # system not whole-down


def test_whole_system_incident_downs_system_and_components(db_session):
    lvl = _sev(db_session)
    active, _closed = _seed_statuses(db_session)
    s = System(name="Billing")
    db_session.add(s)
    db_session.flush()
    a = Component(name="A", system_id=s.id)
    db_session.add(a)
    inc = Incident(title="all down", severity_level_id=lvl.id, status_id=active.id, system_id=s.id)
    # no components → whole system
    db_session.add(inc)
    db_session.flush()
    g = service_map.build_graph(db_session)
    assert _status(g, f"sys-{s.id}") == "down"
    assert _status(g, f"comp-{a.id}") == "down"


def test_system_dep_blast_radius(db_session):
    lvl = _sev(db_session)
    active, _closed = _seed_statuses(db_session)
    s1 = System(name="S1")  # S1 depends on S2
    s2 = System(name="S2")
    db_session.add_all([s1, s2])
    db_session.flush()
    s1.depends_on = [s2]
    inc = Incident(title="S2 down", severity_level_id=lvl.id, status_id=active.id, system_id=s2.id)
    db_session.add(inc)
    db_session.flush()
    g = service_map.build_graph(db_session)
    assert _status(g, f"sys-{s2.id}") == "down"
    assert _status(g, f"sys-{s1.id}") == "impacted"


def test_closed_incident_excluded(db_session):
    lvl = _sev(db_session)
    _active, closed = _seed_statuses(db_session)
    s = System(name="Billing")
    db_session.add(s)
    db_session.flush()
    a = Component(name="A", system_id=s.id)
    db_session.add(a)
    inc = Incident(title="was down", severity_level_id=lvl.id, status_id=closed.id, system_id=s.id)
    inc.components = [a]
    db_session.add(inc)
    db_session.flush()
    g = service_map.build_graph(db_session)
    assert _status(g, f"comp-{a.id}") == "ok"
