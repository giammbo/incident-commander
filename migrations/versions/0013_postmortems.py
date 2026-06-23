"""postmortems"""

from alembic import op
import sqlalchemy as sa

revision = "0013_postmortems"
down_revision = "0012_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "postmortems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id", sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("incident_id", name="uq_postmortems_incident"),
    )


def downgrade() -> None:
    op.drop_table("postmortems")
