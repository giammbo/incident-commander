"""incident roles & assignees"""

from alembic import op
import sqlalchemy as sa

revision = "0007_incident_roles"
down_revision = "0006_status_levels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_role_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("label", name="uq_incident_role_types_label"),
    )
    op.create_table(
        "incident_role_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_type_id",
            sa.Integer(),
            sa.ForeignKey("incident_role_types.id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("assigned_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint(
            "incident_id", "role_type_id", "user_id", name="uq_incident_role_user"
        ),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO incident_role_types (label, rank) VALUES "
            "('Incident Lead',1),('Communications',2),('Scribe',3)"
        )
    )


def downgrade() -> None:
    op.drop_table("incident_role_assignments")
    op.drop_table("incident_role_types")
