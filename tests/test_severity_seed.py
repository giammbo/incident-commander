from sqlalchemy import select

from app.models import SeverityLevel
from app.services.catalog import default_severity_level_id, seed_severity_levels


def test_seed_is_idempotent(db_session):
    seed_severity_levels(db_session)
    db_session.flush()
    seed_severity_levels(db_session)
    db_session.flush()
    rows = list(db_session.scalars(select(SeverityLevel)))
    assert len(rows) == 3
    assert default_severity_level_id(db_session) is not None
