"""systems & components hierarchy

Renames services→components, adds systems + system_dependencies, components.system_id
(backfilled to an auto-created "Default" system when components pre-exist), and
incidents.system_id. Existing incidents are NOT backfilled with a system_id by design
(scope binds only on write); their previously linked components stay attached.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_systems_components"
down_revision = "0002_enrichment"  # confirmed: matches `revision` in 0002_enrichment.py
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "systems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_systems_name"),
    )
    op.create_table(
        "system_dependencies",
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("depends_on_id", sa.Integer(), sa.ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
    )

    # services -> components (table, dependency M2M, incident M2M)
    op.rename_table("services", "components")
    op.rename_table("service_dependencies", "component_dependencies")
    op.alter_column("component_dependencies", "service_id", new_column_name="component_id")
    op.rename_table("incident_services", "incident_components")
    op.alter_column("incident_components", "service_id", new_column_name="component_id")

    # components.system_id (nullable, then backfill, then NOT NULL)
    op.add_column("components", sa.Column("system_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_components_system_id", "components", "systems",
        ["system_id"], ["id"], ondelete="CASCADE",
    )
    conn = op.get_bind()
    has_components = conn.execute(sa.text("SELECT 1 FROM components LIMIT 1")).first()
    if has_components:
        # Idempotent: re-running the backfill branch (e.g. during a down/up verify cycle)
        # must not trip the uq_systems_name unique constraint.
        conn.execute(
            sa.text(
                "INSERT INTO systems (name, description) "
                "SELECT 'Default', 'Auto-created during the systems/components migration' "
                "WHERE NOT EXISTS (SELECT 1 FROM systems WHERE name = 'Default')"
            )
        )
        default_id = conn.execute(
            sa.text("SELECT id FROM systems WHERE name = 'Default'")
        ).scalar_one()
        conn.execute(
            sa.text("UPDATE components SET system_id = :sid WHERE system_id IS NULL"),
            {"sid": default_id},
        )
    op.alter_column("components", "system_id", nullable=False)

    # incidents.system_id (nullable, no backfill)
    op.add_column("incidents", sa.Column("system_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_incidents_system_id", "incidents", "systems", ["system_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_incidents_system_id", "incidents", type_="foreignkey")
    op.drop_column("incidents", "system_id")

    op.drop_constraint("fk_components_system_id", "components", type_="foreignkey")
    op.drop_column("components", "system_id")

    op.alter_column("incident_components", "component_id", new_column_name="service_id")
    op.rename_table("incident_components", "incident_services")
    op.alter_column("component_dependencies", "component_id", new_column_name="service_id")
    op.rename_table("component_dependencies", "service_dependencies")
    op.rename_table("components", "services")

    op.drop_table("system_dependencies")
    op.drop_table("systems")
