# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [0.0.4] — 2026-10-20

### Added
- Dependency editor for components and systems: a checklist that states the direction ("X depends on:"), shows "used by", and disables choices that would close a loop.
- Automations editor built from dropdowns (no numeric IDs); rules are listed as readable sentences.
- Generic JSON inbound integrations get an editable field mapping (dedup key, title, severity, status, resolved value).
- Outgoing webhooks: optional HMAC signing secret (`X-IC-Signature: sha256=…`) and 2 retries on network/5xx errors.

### Changed
- Outgoing webhooks are delivered in the background, so declaring or updating an incident no longer waits on receivers.
- Microsoft Teams webhooks now send a Workflows Adaptive Card instead of the retired O365 MessageCard.
- Incidents promoted from the Alerts inbox or auto-created by an alert rule now run the incident-opened rules and fire the `opened` webhook, like a manual declare; the timeline records which rule created them.
- Incident detail keeps the tab you were working in after saving a note, follow-up, role or update.
- Declaring from the board resets the form and refreshes the open/total counter.
- The SSO button sits inside the login card.

### Fixed
- Circular dependencies between components or systems (direct or indirect) are rejected with the loop path.

### Upgrade notes
- Adds migration `0018_webhook_secret`, applied automatically on startup.

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

[0.0.4]: https://github.com/giammbo/incident-commander/releases/tag/v0.0.4
[Unreleased]: https://github.com/giammbo/incident-commander/commits/main
