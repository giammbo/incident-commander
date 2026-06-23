import pytest
import sqlalchemy

from app.models import SeverityLevel, User
from app.services import followups, postmortems, roles, statuses, timeline
from app.services.incidents import create_incident


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied, advance sequence."""
    db_session.execute(
        sqlalchemy.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sqlalchemy.text("SELECT setval('users_id_seq', 1, true)"))
    db_session.flush()


def _incident(db):
    statuses.seed_status_levels(db)
    roles.seed_incident_role_types(db)
    db.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    inc = create_incident(
        db, title="Checkout down", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db.flush()
    return inc


def test_render_template_includes_sections_and_data(db_session):
    inc = _incident(db_session)
    u = User(email="a@x.io", name="Alice", is_active=True)
    db_session.add(u)
    db_session.flush()
    lead = roles.list_incident_role_types(db_session)[0]
    from app.services.incidents import set_incident_role_assignees

    set_incident_role_assignees(db_session, inc, role_type_id=lead.id, user_ids=[u.id], by_user=1)
    followups.create_followup(db_session, inc, title="Patch deploy", created_by=1)
    timeline.add_note(db_session, inc, body="Mitigation applied", created_by=1)
    db_session.flush()
    md = postmortems.render_template(
        inc,
        events=list(inc.events),
        follow_ups=followups.list_followups(inc),
        roles_by_type=roles.assignments_by_role(inc),
        role_types=roles.list_incident_role_types(db_session),
    )
    assert "# Postmortem: Checkout down" in md
    assert "**Severity:** SEV1" in md and "**Duration:**" in md
    assert "## Timeline" in md and "Mitigation applied" in md
    assert "## Follow-ups" in md and "[ ] Patch deploy" in md  # open => unchecked
    assert "Incident Lead:** Alice" in md
    assert "## Root cause" in md and "## Lessons learned" in md


def test_generate_is_idempotent_and_regenerate_overwrites(db_session):
    inc = _incident(db_session)
    pm = postmortems.generate(db_session, inc, by_user=1)
    first = pm.body
    pm.body = "EDITED"
    db_session.flush()
    again = postmortems.generate(db_session, inc, by_user=1)  # existing -> unchanged
    assert again.id == pm.id and again.body == "EDITED"
    postmortems.regenerate(db_session, pm, inc, by_user=1)  # overwrite from template
    assert pm.body != "EDITED" and pm.body.startswith("# Postmortem:")
    assert pm.body.split("\n", 1)[0] == first.split("\n", 1)[0]


def test_completed_followup_is_checked(db_session):
    inc = _incident(db_session)
    f = followups.create_followup(db_session, inc, title="Done item", created_by=1)
    followups.set_followup_status(db_session, f, status="completed", by_user=1)
    db_session.flush()
    md = postmortems.render_template(
        inc,
        events=list(inc.events),
        follow_ups=followups.list_followups(inc),
        roles_by_type=roles.assignments_by_role(inc),
        role_types=roles.list_incident_role_types(db_session),
    )
    assert "[x] Done item" in md


def test_update_body_rejects_empty(db_session):
    inc = _incident(db_session)
    pm = postmortems.generate(db_session, inc, by_user=1)
    with pytest.raises(ValueError):
        postmortems.update_body(db_session, pm, body="   ", by_user=1)
    postmortems.update_body(db_session, pm, body="new content", by_user=1)
    assert pm.body == "new content" and pm.updated_by == 1
