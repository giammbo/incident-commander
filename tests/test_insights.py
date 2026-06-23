from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import SeverityLevel, StatusCategory, StatusLevel, System
from app.services import insights, statuses
from app.services.followups import create_followup
from app.services.incidents import create_incident, set_incident_status


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied, then advance the sequence."""
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


def _seed(db):
    statuses.seed_status_levels(db)
    db.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(sev)
    db.flush()
    return sev


def test_window_since():
    assert insights.window_since(0) is None
    assert insights.window_since(-1) is None
    s = insights.window_since(30)
    assert s is not None and s < datetime.now(UTC)


def test_counts_mttr_and_breakdowns(db_session):
    sev = _seed(db_session)
    sysm = System(name="Backend")
    db_session.add(sysm)
    db_session.flush()
    closed_status = next(
        s for s in db_session.scalars(sa.select(StatusLevel)) if s.category == StatusCategory.closed
    )
    # two incidents, one closed with a known ~2h duration, one open
    a = create_incident(
        db_session,
        title="A",
        severity_level_id=sev.id,
        is_private=False,
        created_by=1,
        system_id=sysm.id,
    )
    b = create_incident(
        db_session, title="B", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    set_incident_status(db_session, a, status_id=closed_status.id, by_user=1)
    a.created_at = datetime.now(UTC) - timedelta(hours=2)
    a.closed_at = a.created_at + timedelta(hours=2)
    create_followup(db_session, b, title="todo", created_by=1)  # one open follow-up
    db_session.flush()

    data = insights.compute_insights(db_session, since=None)
    assert data["total"] == 2 and data["open"] == 1 and data["closed"] == 1
    assert data["mttr"] == "2h"  # one closed incident, 2h
    assert data["open_followups"] == 1
    sev_counts = {r["label"]: r["count"] for r in data["by_severity"]}
    assert sev_counts == {"SEV1": 2}
    sys_counts = {r["label"]: r["count"] for r in data["by_system"]}
    assert sys_counts.get("Backend") == 1 and sys_counts.get("—") == 1  # b has no system


def test_window_excludes_old(db_session):
    sev = _seed(db_session)
    old = create_incident(
        db_session, title="old", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    old.created_at = datetime.now(UTC) - timedelta(days=120)
    db_session.flush()
    assert insights.compute_insights(db_session, since=insights.window_since(30))["total"] == 0
    assert insights.compute_insights(db_session, since=None)["total"] == 1
