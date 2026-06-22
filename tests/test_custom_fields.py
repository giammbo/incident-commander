import json

import pytest
import sqlalchemy as sa

from app.models import IncidentType, SeverityLevel
from app.services import custom_fields as cf
from app.services.incidents import create_incident


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied."""
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.flush()


def _incident(db, type_label=None):
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    tid = None
    if type_label:
        t = IncidentType(label=type_label, rank=1, is_default=True)
        db.add(t)
        db.flush()
        tid = t.id
    inc = create_incident(
        db,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        incident_type_id=tid,
    )
    db.flush()
    return inc


def test_create_validates_type(db_session):
    with pytest.raises(ValueError):
        cf.create_field_def(db_session, label="Bad", field_type="bogus")
    with pytest.raises(ValueError):
        cf.create_field_def(db_session, label="  ", field_type="text")
    fd = cf.create_field_def(db_session, label="Root cause", field_type="textarea")
    assert fd.id and fd.field_type == "textarea"


def test_fields_for_type_binding(db_session):
    t1 = IncidentType(label="Outage", rank=1)
    t2 = IncidentType(label="Security", rank=2)
    db_session.add_all([t1, t2])
    db_session.flush()
    all_f = cf.create_field_def(db_session, label="Summary", field_type="text")  # no binding -> all
    sec = cf.create_field_def(db_session, label="CVE", field_type="text", incident_type_ids=[t2.id])
    db_session.flush()
    ids_t1 = {f.id for f in cf.fields_for_type(db_session, t1.id)}
    ids_t2 = {f.id for f in cf.fields_for_type(db_session, t2.id)}
    assert all_f.id in ids_t1 and sec.id not in ids_t1
    assert all_f.id in ids_t2 and sec.id in ids_t2


def test_set_values_coercion_and_required(db_session):
    inc = _incident(db_session, type_label="Outage")
    txt = cf.create_field_def(db_session, label="Notes", field_type="text", required=True)
    num = cf.create_field_def(db_session, label="Count", field_type="number")
    ms = cf.create_field_def(db_session, label="Tags", field_type="multi_select", options="a\nb\nc")
    db_session.flush()
    missing = cf.set_incident_values(
        db_session, inc, {txt.id: "  ", num.id: "notnum", ms.id: ["a", "c"]}
    )
    assert "Notes" in missing  # required + empty
    vals = cf.values_for_incident(inc)
    assert num.id not in vals  # non-numeric dropped
    assert json.loads(vals[ms.id]) == ["a", "c"]
    # now fill required + fix number
    missing2 = cf.set_incident_values(
        db_session, inc, {txt.id: "root caused", num.id: "42", ms.id: []}
    )
    assert missing2 == []
    vals2 = cf.values_for_incident(inc)
    assert vals2[txt.id] == "root caused" and vals2[num.id] == "42"
    assert ms.id not in vals2  # empty multi-select removed


def test_display_value(db_session):
    fd = cf.create_field_def(db_session, label="Tags", field_type="multi_select", options="a\nb")
    db_session.flush()
    assert cf.display_value(fd, json.dumps(["a", "b"])) == "a, b"
    cb = cf.create_field_def(db_session, label="Flag", field_type="checkbox")
    db_session.flush()
    assert cf.display_value(cb, "true") == "Yes"
