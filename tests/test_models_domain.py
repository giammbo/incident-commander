from app.models import Incident, IncidentStatus, SeverityLevel


def _sev(db):
    lvl = SeverityLevel(label="SEV2", color="#F4B740", rank=2, is_default=True)
    db.add(lvl)
    db.flush()
    return lvl.id


def test_incident_defaults(db_session):
    inc = Incident(title="Checkout down", severity_level_id=_sev(db_session))
    db_session.add(inc)
    db_session.flush()
    assert inc.status == IncidentStatus.open
    assert inc.is_private is False
    assert inc.creation_state == {}
    assert inc.slack_connection_id is None
    assert inc.google_connection_id is None
    assert inc.severity_level_id is not None
    assert inc.description is None
    assert inc.services == []
