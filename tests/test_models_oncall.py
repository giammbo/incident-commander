from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.models import (
    ContactMethod,
    Escalation,
    EscalationStatus,
    Schedule,
    ScheduleOverride,
    ScheduleRotation,
    ScheduleRotationVersion,
    Urgency,
    User,
)


def _user(db, email="a@example.com"):
    u = User(email=email, name="A")
    db.add(u)
    db.flush()
    return u


def _schedule(db, user):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    s = Schedule(name="Primary", timezone="Europe/Rome")
    rot = ScheduleRotation(name="Daily", rank=1)
    rot.versions.append(
        ScheduleRotationVersion(
            effective_from=now, anchor=now, shift_hours=24, user_ids=[1, 2], windows=[]
        )
    )
    s.rotations.append(rot)
    s.overrides.append(
        ScheduleOverride(user_id=user.id, starts_at=now, ends_at=now + timedelta(hours=4))
    )
    db.add(s)
    db.commit()
    return s


def test_schedule_with_rotation_version_and_override_roundtrip(db_session):
    user = _user(db_session)
    s = _schedule(db_session, user)
    db_session.expire_all()
    s = db_session.get(Schedule, s.id)
    assert s.timezone == "Europe/Rome"
    assert s.rotations[0].versions[0].user_ids == [1, 2]
    assert s.rotations[0].versions[0].shift_hours == 24
    assert s.overrides[0].user_id == user.id


def test_deleting_schedule_cascades_rotations_versions_overrides(db_session):
    user = _user(db_session)
    s = _schedule(db_session, user)
    db_session.delete(db_session.get(Schedule, s.id))
    db_session.commit()
    for model in (ScheduleRotation, ScheduleRotationVersion, ScheduleOverride):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_escalation_defaults(db_session):
    e = Escalation(source="manual", urgency=Urgency.high, title="t", short_code="ab12")
    db_session.add(e)
    db_session.commit()
    assert e.status == EscalationStatus.triggered
    assert e.current_level == 0
    assert e.repeat_count == 0
    assert e.direct_user_ids == []


def test_contact_method_value_is_encrypted_at_rest(db_session):
    user = _user(db_session)
    cm = ContactMethod(user_id=user.id, kind="phone", value="+391234567890")
    db_session.add(cm)
    db_session.commit()
    raw = db_session.execute(
        text("SELECT value FROM contact_methods WHERE id = :i"), {"i": cm.id}
    ).scalar_one()
    assert "+391234" not in raw
    db_session.expire_all()
    assert db_session.get(ContactMethod, cm.id).value == "+391234567890"
