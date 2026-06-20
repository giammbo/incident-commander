# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added — Phase 1: Core platform & auth
- Self-hosted FastAPI + HTMX app, deployable via Docker Compose with Postgres.
- Local authentication (argon2) with a first-boot bootstrap admin whose generated password is printed to the logs.
- Group-based RBAC with three roles: Admin, Incident Commander, Read-only.
- Admin UI to manage users and groups; forced password change on first login.
- Record-only incident lifecycle: declare, list, view, and close incidents (severity SEV1–SEV3, public/private).
- Encrypted settings store (Fernet) and a settings page; secret values never rendered or logged.
- Dark "war room" UI where colour encodes incident severity.

### Notes
- Slack channels, Google Meet, Google SSO, and SMTP email invites are planned for later phases; connection fields exist but integrations are not yet wired.

[Unreleased]: https://github.com/giammbo/incident-commander/commits/main
