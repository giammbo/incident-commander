import pytest
import sqlalchemy as sa

from app.models import Alert, InboundIntegration


def test_integration_and_alert_roundtrip(db_session):
    integ = InboundIntegration(name="prod-sns", kind="sns", token="tok-abc")
    db_session.add(integ)
    db_session.flush()
    a = Alert(
        integration_id=integ.id,
        source="cloudwatch",
        dedup_key="arn:alarm:x",
        title="High CPU",
        status="firing",
        payload={"k": "v"},
        links=[{"label": "cw", "url": "https://x"}],
    )
    db_session.add(a)
    db_session.flush()
    assert a.occurrence_count == 1 and a.status == "firing"
    assert a.first_seen_at is not None and a.links[0]["label"] == "cw"


def test_dedup_unique_per_integration(db_session):
    integ = InboundIntegration(name="i", kind="generic", token="tok-2")
    db_session.add(integ)
    db_session.flush()
    db_session.add(Alert(integration_id=integ.id, source="generic", dedup_key="dk", title="a"))
    db_session.flush()
    db_session.add(Alert(integration_id=integ.id, source="generic", dedup_key="dk", title="b"))
    with pytest.raises(sa.exc.IntegrityError):
        db_session.flush()


def test_deleting_integration_nulls_alert(db_session):
    integ = InboundIntegration(name="i", kind="generic", token="tok-3")
    db_session.add(integ)
    db_session.flush()
    a = Alert(integration_id=integ.id, source="generic", dedup_key="dk2", title="a")
    db_session.add(a)
    db_session.flush()
    db_session.delete(integ)
    db_session.flush()
    db_session.refresh(a)
    assert a.integration_id is None  # ON DELETE SET NULL
