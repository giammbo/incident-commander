"""teams & ownership"""

from alembic import op
import sqlalchemy as sa

revision = "0012_teams"
down_revision = "0011_custom_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_teams_name"),
    )
    op.add_column("systems", sa.Column("owner_team_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_systems_owner_team_id", "systems", "teams", ["owner_team_id"], ["id"]
    )
    op.add_column("components", sa.Column("owner_team_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_components_owner_team_id", "components", "teams", ["owner_team_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_components_owner_team_id", "components", type_="foreignkey")
    op.drop_column("components", "owner_team_id")
    op.drop_constraint("fk_systems_owner_team_id", "systems", type_="foreignkey")
    op.drop_column("systems", "owner_team_id")
    op.drop_table("teams")
