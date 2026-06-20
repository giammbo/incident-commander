from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
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
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_protected_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    groups: Mapped[list[Group]] = relationship(secondary="user_groups", back_populates="members")


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


class IncidentStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class SeverityLevel(Base):
    __tablename__ = "severity_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(40), unique=True)
    color: Mapped[str] = mapped_column(String(9))  # hex like #FF5D5D
    rank: Mapped[int] = mapped_column(Integer, default=100)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


service_dependencies = Table(
    "service_dependencies",
    Base.metadata,
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)

incident_services = Table(
    "incident_services",
    Base.metadata,
    Column("incident_id", ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)


class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    depends_on: Mapped[list[Service]] = relationship(
        "Service",
        secondary=service_dependencies,
        primaryjoin=lambda: Service.id == service_dependencies.c.service_id,
        secondaryjoin=lambda: Service.id == service_dependencies.c.depends_on_id,
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


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity_level_id: Mapped[int | None] = mapped_column(
        ForeignKey("severity_levels.id"), nullable=True
    )
    severity_level: Mapped[SeverityLevel | None] = relationship("SeverityLevel")
    services: Mapped[list[Service]] = relationship("Service", secondary=incident_services)
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, name="incident_status"), default=IncidentStatus.open
    )
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
    creation_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
