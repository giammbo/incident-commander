"""Bounded, safe last actual automation execution outcome."""

import sqlalchemy as sa
from alembic import op

revision = "0020_automation_outcomes"
down_revision = "0019_oncall"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_rules", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("workflow_rules", sa.Column("last_run_outcome", sa.String(24), nullable=True))
    op.add_column("workflow_rules", sa.Column("last_run_summary", sa.String(300), nullable=True))


def downgrade():
    op.drop_column("workflow_rules", "last_run_summary")
    op.drop_column("workflow_rules", "last_run_outcome")
    op.drop_column("workflow_rules", "last_run_at")
