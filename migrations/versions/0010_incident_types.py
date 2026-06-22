"""incident types"""

from alembic import op
import sqlalchemy as sa

revision = "0010_incident_types"
down_revision = "0009_follow_ups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "default_severity_level_id",
            sa.Integer(),
            sa.ForeignKey("severity_levels.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("label", name="uq_incident_types_label"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO incident_types (label, rank, is_default) VALUES "
            "('Outage',1,true),('Degraded performance',2,false),"
            "('Maintenance',3,false),('Security',4,false)"
        )
    )
    op.add_column("incidents", sa.Column("incident_type_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_incidents_incident_type_id", "incidents", "incident_types",
        ["incident_type_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_incidents_incident_type_id", "incidents", type_="foreignkey")
    op.drop_column("incidents", "incident_type_id")
    op.drop_table("incident_types")
