"""configurable incident lifecycle statuses"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_status_levels"
down_revision = "0005_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_table emits CREATE TYPE status_category exactly once (matches the 0005 pattern).
    op.create_table(
        "status_levels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column(
            "category",
            sa.Enum("triage", "active", "closed", name="status_category"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("label", name="uq_status_levels_label"),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO status_levels (label, category, rank, is_default) VALUES "
            "('Triage','triage',1,true),('Investigating','active',2,false),"
            "('Identified','active',3,false),('Monitoring','active',4,false),"
            "('Closed','closed',5,false)"
        )
    )
    op.add_column("incidents", sa.Column("status_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_incidents_status_id", "incidents", "status_levels", ["status_id"], ["id"]
    )
    # Backfill from the old enum: open -> Investigating (an active status, since these were
    # already active — NOT the new-incident default Triage); closed -> Closed.
    conn.execute(
        sa.text(
            "UPDATE incidents SET status_id = "
            "(SELECT id FROM status_levels WHERE label='Investigating') WHERE status = 'open'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE incidents SET status_id = "
            "(SELECT id FROM status_levels WHERE label='Closed') WHERE status = 'closed'"
        )
    )
    # status_id stays NULLABLE — matches the ORM (models.py: nullable=True) and the null-safe
    # service_map OUTER JOIN. create_incident always sets a default, so live rows are never null.
    op.drop_column("incidents", "status")
    sa.Enum(name="incident_status").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    conn = op.get_bind()
    # Recreate the old enum explicitly; create_type=False keeps add_column from re-emitting it.
    status_enum = postgresql.ENUM("open", "closed", name="incident_status", create_type=False)
    sa.Enum("open", "closed", name="incident_status").create(conn, checkfirst=True)
    op.add_column("incidents", sa.Column("status", status_enum, nullable=True))
    conn.execute(
        sa.text(
            "UPDATE incidents SET status = (CASE WHEN status_id IN "
            "(SELECT id FROM status_levels WHERE category='closed') "
            "THEN 'closed' ELSE 'open' END)::incident_status"
        )
    )
    op.alter_column("incidents", "status", nullable=False)
    op.drop_constraint("fk_incidents_status_id", "incidents", type_="foreignkey")
    op.drop_column("incidents", "status_id")
    op.drop_table("status_levels")
    sa.Enum(name="status_category").drop(op.get_bind(), checkfirst=True)
