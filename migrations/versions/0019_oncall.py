"""on-call: schedules, escalation paths, routes, escalations, notifications"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import app.crypto

revision = "0019_oncall"
down_revision = "0018_webhook_secret"
branch_labels = None
depends_on = None

urgency = postgresql.ENUM("high", "low", name="urgency", create_type=False)
escalation_status = postgresql.ENUM(
    "triggered",
    "acknowledged",
    "resolved",
    "exhausted",
    name="escalation_status",
    create_type=False,
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    bind = op.get_bind()
    urgency.create(bind, checkfirst=True)
    escalation_status.create(bind, checkfirst=True)
    op.add_column("alerts", sa.Column("paging_error", sa.Text(), nullable=True))

    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        _created_at(),
    )
    op.create_table(
        "schedule_rotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.Integer(),
            sa.ForeignKey("schedules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
    )
    op.create_table(
        "schedule_rotation_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rotation_id",
            sa.Integer(),
            sa.ForeignKey("schedule_rotations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_hours", sa.Integer(), nullable=False),
        sa.Column("user_ids", postgresql.JSONB(), nullable=False),
        sa.Column("windows", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "schedule_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.Integer(),
            sa.ForeignKey("schedules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        _created_at(),
    )
    op.create_table(
        "escalation_paths",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("repeat_times", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "escalation_levels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "path_id",
            sa.Integer(),
            sa.ForeignKey("escalation_paths.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("ack_timeout_minutes", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("schedule_ids", postgresql.JSONB(), nullable=False),
        sa.Column("user_ids", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "alert_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("conditions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "escalation_path_id", sa.Integer(), sa.ForeignKey("escalation_paths.id"), nullable=False
        ),
        sa.Column("urgency", urgency, nullable=False, server_default="high"),
        sa.Column("high_if", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", escalation_status, nullable=False),
        sa.Column("urgency", urgency, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("short_code", sa.String(4), nullable=False),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="SET NULL")),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id", ondelete="SET NULL")),
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("alert_routes.id", ondelete="SET NULL")),
        sa.Column(
            "path_id", sa.Integer(), sa.ForeignKey("escalation_paths.id", ondelete="SET NULL")
        ),
        sa.Column("direct_user_ids", postgresql.JSONB(), nullable=False),
        sa.Column("current_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repeat_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initiated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        _created_at(),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_escalations_alert_status", "escalations", ["alert_id", "status"])
    op.create_table(
        "escalation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "escalation_id",
            sa.Integer(),
            sa.ForeignKey("escalations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        _created_at(),
    )
    op.create_table(
        "notification_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "escalation_id",
            sa.Integer(),
            sa.ForeignKey("escalations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notification_jobs_pending",
        "notification_jobs",
        ["run_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "contact_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", app.crypto.EncryptedString(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_hash", sa.String(64), nullable=True),
        sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_table(
        "notification_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("urgency", urgency, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("delay_minutes", sa.Integer(), nullable=False),
    )
    op.create_table(
        "oncall_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("twilio_account_sid", sa.String(64), nullable=True),
        sa.Column("twilio_auth_token", app.crypto.EncryptedString(), nullable=True),
        sa.Column("twilio_from_number", sa.String(32), nullable=True),
        sa.Column("vapid_public_key", sa.Text(), nullable=True),
        sa.Column("vapid_private_key", app.crypto.EncryptedString(), nullable=True),
        sa.Column("worker_last_tick_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "paging_error")
    op.drop_table("oncall_settings")
    op.drop_table("notification_rules")
    op.drop_table("contact_methods")
    op.drop_index("ix_notification_jobs_pending", table_name="notification_jobs")
    op.drop_table("notification_jobs")
    op.drop_table("escalation_events")
    op.drop_index("ix_escalations_alert_status", table_name="escalations")
    op.drop_table("escalations")
    op.drop_table("alert_routes")
    op.drop_table("escalation_levels")
    op.drop_table("escalation_paths")
    op.drop_table("schedule_overrides")
    op.drop_table("schedule_rotation_versions")
    op.drop_table("schedule_rotations")
    op.drop_table("schedules")
    bind = op.get_bind()
    escalation_status.drop(bind, checkfirst=True)
    urgency.drop(bind, checkfirst=True)
