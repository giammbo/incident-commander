"""outgoing webhooks"""

from alembic import op
import sqlalchemy as sa
import app.crypto

revision = "0005_webhooks"
down_revision = "0004_oidc_sso"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("url", app.crypto.EncryptedString(), nullable=False),
        sa.Column(
            "format",
            sa.Enum("slack", "teams", "discord", "generic", name="webhook_format"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("webhooks")
    sa.Enum(name="webhook_format").drop(op.get_bind(), checkfirst=True)
