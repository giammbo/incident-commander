import pytest
import sqlalchemy as sa

from app.models import SeverityLevel, StatusCategory, StatusLevel, WorkflowRule
from app.services import automation, statuses
from app.services.incidents import create_incident


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


def _sev(db, label="SEV1"):
    s = SeverityLevel(label=label, color="#FF5D5D", rank=1, is_default=True)
    db.add(s)
    db.flush()
    return s


def _incident(db, sev):
    statuses.seed_status_levels(db)
    db.flush()
    inc = create_incident(db, title="X", severity_level_id=sev.id, is_private=False, created_by=1)
    db.flush()
    return inc


def test_matches_equals_and_in():
    class S:  # stand-in subject
        severity_level_id = 5
        is_private = False

    assert automation._matches([{"field": "severity", "op": "equals", "value": 5}], S)
    assert not automation._matches([{"field": "severity", "op": "equals", "value": 9}], S)
    assert automation._matches([{"field": "severity", "op": "in", "value": [4, 5]}], S)
    assert not automation._matches([{"field": "severity", "op": "in", "value": [4, 6]}], S)
    # AND: both must hold
    assert not automation._matches(
        [
            {"field": "severity", "op": "equals", "value": 5},
            {"field": "is_private", "op": "equals", "value": True},
        ],
        S,
    )


def test_unknown_field_or_none_does_not_match():
    class S:
        severity_level_id = None
        incident_type_id = None

    assert not automation._matches([{"field": "nope", "op": "equals", "value": 1}], S)
    assert not automation._matches([{"field": "severity", "op": "equals", "value": 1}], S)
    # a None comparison value is misconfigured, NOT a wildcard for unset fields
    assert not automation._matches([{"field": "type", "op": "equals", "value": None}], S)


def test_run_rules_executes_matching_actions(db_session):
    sev = _sev(db_session)
    inc = _incident(db_session, sev)
    monitoring = next(
        s
        for s in db_session.scalars(sa.select(StatusLevel))
        if s.category == StatusCategory.active and s.id != inc.status_id
    )
    db_session.add(
        WorkflowRule(
            name="r",
            trigger="incident.opened",
            conditions=[{"field": "severity", "op": "equals", "value": sev.id}],
            actions=[
                {"type": "set_status", "params": {"status_id": monitoring.id}},
                {"type": "create_followup", "params": {"title": "write postmortem"}},
            ],
        )
    )
    db_session.flush()
    automation.run_rules(db_session, trigger="incident.opened", incident=inc, by_user=1)
    assert inc.status_id == monitoring.id  # set_status action ran
    assert any(f.title == "write postmortem" for f in inc.follow_ups)  # create_followup ran
    # one automation timeline event logged for the fired rule
    from app.models import IncidentEvent

    autos = list(
        db_session.scalars(
            sa.select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == "automation"
            )
        )
    )
    assert len(autos) == 1


def test_non_matching_rule_does_nothing(db_session):
    sev = _sev(db_session)
    inc = _incident(db_session, sev)
    db_session.add(
        WorkflowRule(
            name="r",
            trigger="incident.opened",
            conditions=[{"field": "severity", "op": "equals", "value": sev.id + 999}],
            actions=[{"type": "create_followup", "params": {"title": "nope"}}],
        )
    )
    db_session.flush()
    automation.run_rules(db_session, trigger="incident.opened", incident=inc, by_user=1)
    assert inc.follow_ups == []


def test_failing_action_is_caught_and_next_runs(db_session):
    sev = _sev(db_session)
    inc = _incident(db_session, sev)
    db_session.add(
        WorkflowRule(
            name="r",
            trigger="incident.opened",
            conditions=[],
            actions=[
                {
                    "type": "assign_role",
                    "params": {"role_type_id": 99999, "user_ids": [1]},
                },  # raises (unknown role)
                {"type": "create_followup", "params": {"title": "still runs"}},
            ],
        )
    )
    db_session.flush()
    automation.run_rules(db_session, trigger="incident.opened", incident=inc, by_user=1)
    assert any(
        f.title == "still runs" for f in inc.follow_ups
    )  # 2nd action ran despite 1st failing
