"""webhook signing secret"""

from alembic import op
import sqlalchemy as sa
import app.crypto

revision = "0018_webhook_secret"
down_revision = "0017_meet_service_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhooks", sa.Column("secret", app.crypto.EncryptedString(), nullable=True))


def downgrade() -> None:
    op.drop_column("webhooks", "secret")
