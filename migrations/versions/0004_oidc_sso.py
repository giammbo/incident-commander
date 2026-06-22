"""generic OIDC SSO

Renames users.google_sub→oidc_subject, adds users.oidc_issuer (+ composite unique
(oidc_issuer, oidc_subject)), adds sso_settings issuer/client_id/client_secret/display_name.
Backfills oidc_issuer for existing SSO users (they were Google).
"""

from alembic import op
import sqlalchemy as sa
import app.crypto

revision = "0004_oidc_sso"
down_revision = "0003_systems_components"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sso_settings", sa.Column("issuer", sa.String(length=512), nullable=True))
    op.add_column("sso_settings", sa.Column("client_id", sa.String(length=255), nullable=True))
    op.add_column("sso_settings", sa.Column("client_secret", app.crypto.EncryptedString(), nullable=True))
    op.add_column("sso_settings", sa.Column("display_name", sa.String(length=120), nullable=True))

    op.alter_column("users", "google_sub", new_column_name="oidc_subject")
    op.add_column("users", sa.Column("oidc_issuer", sa.String(length=512), nullable=True))
    op.execute(
        "UPDATE users SET oidc_issuer = 'https://accounts.google.com' "
        "WHERE oidc_subject IS NOT NULL"
    )
    # the old single-column unique was created by 0001 as users_google_sub_key
    op.drop_constraint("users_google_sub_key", "users", type_="unique")
    op.create_unique_constraint("uq_users_oidc_identity", "users", ["oidc_issuer", "oidc_subject"])


def downgrade() -> None:
    op.drop_constraint("uq_users_oidc_identity", "users", type_="unique")
    op.drop_column("users", "oidc_issuer")
    op.alter_column("users", "oidc_subject", new_column_name="google_sub")
    op.create_unique_constraint("users_google_sub_key", "users", ["google_sub"])
    for col in ("display_name", "client_secret", "client_id", "issuer"):
        op.drop_column("sso_settings", col)
