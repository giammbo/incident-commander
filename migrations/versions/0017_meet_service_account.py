"""meet service account fields"""

from alembic import op
import sqlalchemy as sa

revision = "0017_meet_service_account"
down_revision = "0016_incident_gemini_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("meet_space_name", sa.String(length=255), nullable=True))
    op.add_column("google_settings", sa.Column("service_account_json", sa.Text(), nullable=True))
    op.add_column("google_settings", sa.Column("impersonate_email", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("google_settings", "impersonate_email")
    op.drop_column("google_settings", "service_account_json")
    op.drop_column("incidents", "meet_space_name")
