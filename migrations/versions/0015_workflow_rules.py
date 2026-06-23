"""workflow rules"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_workflow_rules"
down_revision = "0014_inbound_alerting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("conditions", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("actions", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("rank", sa.Integer(), server_default="100", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workflow_rules_trigger", "workflow_rules", ["trigger"])


def downgrade() -> None:
    op.drop_table("workflow_rules")
