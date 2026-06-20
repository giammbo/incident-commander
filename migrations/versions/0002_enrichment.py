import sqlalchemy as sa
from alembic import op

revision = "0002_enrichment"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_SEED = [("SEV1", "#FF5D5D", 1, False), ("SEV2", "#F4B740", 2, True), ("SEV3", "#56B6E6", 3, False)]


def upgrade():
    sl = op.create_table(
        "severity_levels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.String(40), nullable=False, unique=True),
        sa.Column("color", sa.String(9), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False, server_default="100"),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.bulk_insert(
        sl,
        [{"label": l, "color": c, "rank": r, "is_default": d} for (l, c, r, d) in _SEED],
    )
    op.create_table(
        "services",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "service_dependencies",
        sa.Column(
            "service_id",
            sa.Integer,
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "depends_on_id",
            sa.Integer,
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "incident_services",
        sa.Column(
            "incident_id",
            sa.Integer,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "service_id",
            sa.Integer,
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.add_column("incidents", sa.Column("description", sa.Text, nullable=True))
    op.add_column(
        "incidents",
        sa.Column("severity_level_id", sa.Integer, sa.ForeignKey("severity_levels.id"), nullable=True),
    )
    # backfill: map the old enum value to the seeded level label
    op.execute("""
        UPDATE incidents i SET severity_level_id = s.id
        FROM severity_levels s WHERE s.label = i.severity::text
    """)
    op.drop_column("incidents", "severity")
    op.execute("DROP TYPE IF EXISTS severity")


def downgrade():
    severity = sa.Enum("SEV1", "SEV2", "SEV3", name="severity")
    severity.create(op.get_bind(), checkfirst=True)
    op.add_column("incidents", sa.Column("severity", severity, nullable=True))
    op.execute("""
        UPDATE incidents i SET severity = s.label::severity
        FROM severity_levels s WHERE s.id = i.severity_level_id AND s.label IN ('SEV1','SEV2','SEV3')
    """)
    op.drop_column("incidents", "severity_level_id")
    op.drop_column("incidents", "description")
    op.drop_table("incident_services")
    op.drop_table("service_dependencies")
    op.drop_table("services")
    op.drop_table("severity_levels")
