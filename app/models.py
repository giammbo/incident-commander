from __future__ import annotations

import enum
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.crypto import EncryptedString
from app.db import Base


class Role(str, enum.Enum):
    admin = "admin"
    incident_commander = "incident_commander"
    read_only = "read_only"


ROLE_RANK: dict[Role, int] = {
    Role.read_only: 1,
    Role.incident_commander: 2,
    Role.admin: 3,
}

# Shared Postgres enum object — used by both Group.role and SsoSettings.auto_provision_role
# to avoid "type 'role' already exists" during create_all.
role_enum = sa.Enum(Role, name="role")


class UserGroup(Base):
    __tablename__ = "user_groups"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[Role] = mapped_column(role_enum)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    members: Mapped[list[User]] = relationship(secondary="user_groups", back_populates="groups")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_protected_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    groups: Mapped[list[Group]] = relationship(secondary="user_groups", back_populates="members")
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
    )


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def effective_role(user: User) -> Role | None:
    roles = [g.role for g in user.groups]
    if not roles:
        return None
    return max(roles, key=lambda r: ROLE_RANK[r])


class StatusCategory(str, enum.Enum):
    triage = "triage"
    active = "active"
    closed = "closed"


class StatusLevel(Base):
    __tablename__ = "status_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(40), unique=True)
    category: Mapped[StatusCategory] = mapped_column(Enum(StatusCategory, name="status_category"))
    rank: Mapped[int] = mapped_column(Integer, default=100)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SeverityLevel(Base):
    __tablename__ = "severity_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(40), unique=True)
    color: Mapped[str] = mapped_column(String(9))  # hex like #FF5D5D
    rank: Mapped[int] = mapped_column(Integer, default=100)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentType(Base):
    __tablename__ = "incident_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=100)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    default_severity_level_id: Mapped[int | None] = mapped_column(
        ForeignKey("severity_levels.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    default_severity: Mapped[SeverityLevel | None] = relationship()


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


system_dependencies = Table(
    "system_dependencies",
    Base.metadata,
    Column("system_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
)


class System(Base):
    __tablename__ = "systems"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    owner_team: Mapped[Team | None] = relationship(foreign_keys=[owner_team_id])
    # No ORM delete-orphan cascade: components are reassigned by changing system_id, and
    # delete_system is blocked while non-empty. The components.system_id FK uses
    # ondelete=CASCADE only as a DB-level safety net.
    components: Mapped[list[Component]] = relationship("Component", back_populates="system")
    depends_on: Mapped[list[System]] = relationship(
        "System",
        secondary=system_dependencies,
        primaryjoin=lambda: System.id == system_dependencies.c.system_id,
        secondaryjoin=lambda: System.id == system_dependencies.c.depends_on_id,
    )


component_dependencies = Table(
    "component_dependencies",
    Base.metadata,
    Column("component_id", ForeignKey("components.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("components.id", ondelete="CASCADE"), primary_key=True),
)

incident_components = Table(
    "incident_components",
    Base.metadata,
    Column("incident_id", ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True),
    Column("component_id", ForeignKey("components.id", ondelete="CASCADE"), primary_key=True),
)


class Component(Base):
    __tablename__ = "components"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    system_id: Mapped[int] = mapped_column(
        ForeignKey("systems.id", ondelete="CASCADE"), nullable=False
    )
    owner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    owner_team: Mapped[Team | None] = relationship(foreign_keys=[owner_team_id])
    system: Mapped[System] = relationship("System", back_populates="components")
    depends_on: Mapped[list[Component]] = relationship(
        "Component",
        secondary=component_dependencies,
        primaryjoin=lambda: Component.id == component_dependencies.c.component_id,
        secondaryjoin=lambda: Component.id == component_dependencies.c.depends_on_id,
    )


class SlackConnection(Base):
    __tablename__ = "slack_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    team_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_token: Mapped[str] = mapped_column(EncryptedString)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enterprise_install: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GoogleConnection(Base):
    __tablename__ = "google_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_email: Mapped[str] = mapped_column(String(320), index=True)
    account_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    refresh_token: Mapped[str] = mapped_column(EncryptedString)
    calendar_id: Mapped[str] = mapped_column(String(255), default="primary")
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentRoleType(Base):
    __tablename__ = "incident_role_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(60), unique=True)
    rank: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentRoleAssignment(Base):
    __tablename__ = "incident_role_assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    role_type_id: Mapped[int] = mapped_column(ForeignKey("incident_role_types.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    incident: Mapped[Incident] = relationship(back_populates="role_assignments")
    role_type: Mapped[IncidentRoleType] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    __table_args__ = (
        UniqueConstraint("incident_id", "role_type_id", "user_id", name="uq_incident_role_user"),
    )


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity_level_id: Mapped[int | None] = mapped_column(
        ForeignKey("severity_levels.id"), nullable=True
    )
    severity_level: Mapped[SeverityLevel | None] = relationship("SeverityLevel")
    components: Mapped[list[Component]] = relationship("Component", secondary=incident_components)
    system_id: Mapped[int | None] = mapped_column(ForeignKey("systems.id"), nullable=True)
    system: Mapped[System | None] = relationship("System")
    status_id: Mapped[int | None] = mapped_column(ForeignKey("status_levels.id"), nullable=True)
    status: Mapped[StatusLevel | None] = relationship("StatusLevel")
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    slack_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("slack_connections.id"), nullable=True
    )
    google_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("google_connections.id"), nullable=True
    )
    slack_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_channel_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    slack_channel_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    meet_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gemini_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    creation_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    role_assignments: Mapped[list[IncidentRoleAssignment]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    follow_ups: Mapped[list[FollowUp]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    incident_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_types.id"), nullable=True
    )
    incident_type: Mapped[IncidentType | None] = relationship(foreign_keys=[incident_type_id])
    custom_values: Mapped[list[IncidentCustomFieldValue]] = relationship(
        cascade="all, delete-orphan"
    )
    postmortem: Mapped[Postmortem | None] = relationship(
        cascade="all, delete-orphan", uselist=False
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="incident")

    @property
    def is_closed(self) -> bool:
        return self.status is not None and self.status.category == StatusCategory.closed


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    entry_type: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    incident: Mapped[Incident] = relationship(back_populates="events")
    created_by_user: Mapped[User | None] = relationship(foreign_keys=[created_by])


class FollowUp(Base):
    __tablename__ = "follow_ups"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    incident: Mapped[Incident] = relationship(back_populates="follow_ups")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class _SingletonSettings(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True, default=1)


class SlackSettings(_SingletonSettings):
    __tablename__ = "slack_settings"
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    signing_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)


class GoogleSettings(_SingletonSettings):
    __tablename__ = "google_settings"
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)


class SsoSettings(_SingletonSettings):
    __tablename__ = "sso_settings"
    sso_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_local_login: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_domains: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    auto_provision_role: Mapped[Role] = mapped_column(role_enum, default=Role.read_only)
    issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)


class SmtpSettings(_SingletonSettings):
    __tablename__ = "smtp_settings"
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    use_tls: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSettings(_SingletonSettings):
    __tablename__ = "app_settings"
    slack_channel_name_template: Mapped[str] = mapped_column(
        String(120), default="inc-{date}-{slug}"
    )


class WebhookFormat(str, enum.Enum):
    slack = "slack"
    teams = "teams"
    discord = "discord"
    generic = "generic"


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(EncryptedString)
    format: Mapped[WebhookFormat] = mapped_column(
        Enum(WebhookFormat, name="webhook_format"), default=WebhookFormat.generic
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


custom_field_types = Table(
    "custom_field_types",
    Base.metadata,
    Column(
        "custom_field_id", ForeignKey("custom_field_defs.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "incident_type_id", ForeignKey("incident_types.id", ondelete="CASCADE"), primary_key=True
    ),
)


class CustomFieldDef(Base):
    __tablename__ = "custom_field_defs"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80), unique=True)
    field_type: Mapped[str] = mapped_column(String(16))
    options: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    rank: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    incident_types: Mapped[list[IncidentType]] = relationship(secondary=custom_field_types)

    @property
    def options_list(self) -> list[str]:
        return [o.strip() for o in (self.options or "").splitlines() if o.strip()]


class IncidentCustomFieldValue(Base):
    __tablename__ = "incident_custom_field_values"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    field_id: Mapped[int] = mapped_column(ForeignKey("custom_field_defs.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(Text)
    field: Mapped[CustomFieldDef] = relationship()
    __table_args__ = (UniqueConstraint("incident_id", "field_id", name="uq_incident_field"),)


class Postmortem(Base):
    __tablename__ = "postmortems"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), unique=True
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class InboundIntegration(Base):
    __tablename__ = "inbound_integrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))  # sns | alertmanager | generic
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_integrations.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20))
    dedup_key: Mapped[str] = mapped_column(String(500), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity_raw: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="firing", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    links: Mapped[list] = mapped_column(JSONB, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    integration: Mapped[InboundIntegration | None] = relationship()
    incident: Mapped[Incident | None] = relationship(back_populates="alerts")
    __table_args__ = (UniqueConstraint("integration_id", "dedup_key", name="uq_alert_dedup"),)


class WorkflowRule(Base):
    __tablename__ = "workflow_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger: Mapped[str] = mapped_column(String(40), index=True)
    conditions: Mapped[list] = mapped_column(JSONB, default=list)
    actions: Mapped[list] = mapped_column(JSONB, default=list)
    rank: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
