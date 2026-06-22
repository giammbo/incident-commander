"""custom fields"""

from alembic import op
import sqlalchemy as sa

revision = "0011_custom_fields"
down_revision = "0010_incident_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_field_defs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("field_type", sa.String(length=16), nullable=False),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("label", name="uq_custom_field_defs_label"),
    )
    op.create_table(
        "custom_field_types",
        sa.Column(
            "custom_field_id", sa.Integer(),
            sa.ForeignKey("custom_field_defs.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "incident_type_id", sa.Integer(),
            sa.ForeignKey("incident_types.id", ondelete="CASCADE"), primary_key=True,
        ),
    )
    op.create_table(
        "incident_custom_field_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id", sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "field_id", sa.Integer(),
            sa.ForeignKey("custom_field_defs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("value", sa.Text(), nullable=False),
        sa.UniqueConstraint("incident_id", "field_id", name="uq_incident_field"),
    )


def downgrade() -> None:
    op.drop_table("incident_custom_field_values")
    op.drop_table("custom_field_types")
    op.drop_table("custom_field_defs")
