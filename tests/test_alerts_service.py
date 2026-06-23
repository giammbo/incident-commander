import sqlalchemy as sa

from app.models import Alert, InboundIntegration
from app.services import alerts


def _integ(db):
    integ = InboundIntegration(name="i", kind="generic", token="tk")
    db.add(integ)
    db.flush()
    return integ


def _n(dedup="dk", status="firing"):
    return {
        "source": "generic",
        "dedup_key": dedup,
        "title": "T",
        "description": None,
        "severity_raw": "warn",
        "status": status,
        "links": [],
        "payload": {"x": 1},
    }


def test_ingest_creates_then_dedupes(db_session):
    integ = _integ(db_session)
    a1 = alerts.ingest_alert(db_session, integ, _n())
    a2 = alerts.ingest_alert(db_session, integ, _n())
    assert a1.id == a2.id
    assert a2.occurrence_count == 2 and a2.status == "firing"
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Alert)) == 1


def test_ingest_resolve_then_reopen(db_session):
    integ = _integ(db_session)
    alerts.ingest_alert(db_session, integ, _n())
    resolved = alerts.ingest_alert(db_session, integ, _n(status="resolved"))
    assert resolved.status == "resolved" and resolved.resolved_at is not None
    reopened = alerts.ingest_alert(db_session, integ, _n(status="firing"))
    assert reopened.status == "firing" and reopened.resolved_at is None


def test_list_alerts_filter(db_session):
    integ = _integ(db_session)
    alerts.ingest_alert(db_session, integ, _n(dedup="a"))
    alerts.ingest_alert(db_session, integ, _n(dedup="b", status="resolved"))
    assert len(alerts.list_alerts(db_session)) == 2
    assert len(alerts.list_alerts(db_session, status="resolved")) == 1
