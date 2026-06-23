"""incident gemini notes"""

from alembic import op
import sqlalchemy as sa

revision = "0016_incident_gemini_notes"
down_revision = "0015_workflow_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("gemini_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "gemini_notes")
