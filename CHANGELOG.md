# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

## [0.1.0] — 2026-10-30

### Added
- In-place Settings catalog, group, webhook and automation editing; webhook disable/re-enable and local payload previews; automation condition dry runs and latest actual execution outcomes.
- Catalog search, owner/system filters, pagination and component detail pages; an accessible Maps list with directed relationships, catalog/incident links, search, fit/reset, stale/error feedback and retry.
- Invitation resend/revoke, account reactivation/reset recovery, and explicit push-subscription progress/errors.
- **On-call & paging**: schedules (rotations, DST-safe, windows, overrides, "Take this shift"), multi-level escalation paths with acknowledge timeouts and repeats, alert routing rules (`/oncall/routes`), and manual paging (`/oncall/page`, also from an incident).
- Notification channels: email, Slack DM with Acknowledge/Escalate buttons (`POST /slack/interactions`), SMS and voice via Twilio (`/oncall/twilio/sms`, per-call voice webhooks), and browser push (VAPID). Per-user contact methods with phone verification, per-urgency notification rules, and "send me a test" under **Account → Notifications**.
- Signed acknowledge links (`/oncall/ack/{token}`, confirm page + CSRF-protected POST); a resolved alert from the source closes its escalation automatically; exhausted escalations stay open.
- **Settings → On-call** card (Twilio, VAPID keys, Slack setup instructions); alerts that fail to page show a "paging failed" chip.
- Background on-call worker: in-process by default, optionally a separate process (`entrypoint.sh worker`, a Compose service, Helm `oncall.worker.separate`); `/healthz` reports `oncall_worker` (`ok|stale|never|unknown`) and the UI warns when it is stale.
- Smoke test extended: creates a schedule and path, sends a manual page and checks the worker processed it.

### Changed
- Private incidents are restricted to active admins, their active creator and active explicit role assignees, consistently across detail/actions, alerts, follow-ups, exports, Insights, Maps and paging. Paging alone never grants incident access; recipients are rechecked before delivery and acknowledgement.
- Declaration opens incident detail; board filters survive actions. Response controls remain above tabs, closed incidents emphasize postmortems/follow-ups, and global follow-ups support edit and status changes. Alert promotion requires explicit incident severity and required custom fields.
- Settings saves validate structure without contacting providers, retain existing blank secrets and preserve nonsecret invalid-form values. Automations require a complete action and retain unaffected actions when editing legacy multi-action rules.
- Insights distinguishes the selected creation cohort from current backlog. MTTR includes currently closed cohort incidents with valid timestamps, excludes reopened incidents and shows its sample size.
- Navigation is grouped by Response/Learn/Catalog/Admin, with mobile menu, labelled records or deliberate table scrolling, clearer focus/contrast and quiet configuration cards.
- New dependency `pywebpush`.

### Fixed
- Browser 403/404/422 errors now provide calm HTML recovery without reflecting exception details, submitted passwords/tokens or arbitrary back links. HTMX errors target shared feedback explicitly; bound form errors retain drafts. JSON/API, signed callback and redirect contracts are preserved.
- Required and trimmed text/custom-field validation prevents empty or invalid records while preserving correctable form values.
- Verified phone contacts survive replacement send/verification failures. Push setup uses its root-scope worker registration with activation timeout and visible recovery.
- Login preserves safe local destinations through password changes and SSO. Incomplete SSO configurations do not offer a login button; disabled accounts cannot be reactivated by an OIDC callback.
- Recovery operations serialize per account; overlapping successful sends leave the latest delivered link valid, while failed sends preserve prior valid links and credentials.
- Conditional on-call route fields no longer block the inactive branch; current/future rotations show separate effective dates.

### Upgrade notes
- Adds migration `0020_automation_outcomes` after `0019_oncall`, applied automatically on startup. Latest-execution fields are nullable; existing rules show no recorded outcome until their next actual execution.
- Adds migration `0019_oncall`, applied automatically on startup.
- Existing Slack workspaces must be **reinstalled** (Add to Slack) to grant the new bot scopes (`chat:write`, `channels:manage`, `groups:write`, `im:write`, `users:read`, `users:read.email`), and the Slack app needs **Interactivity → Request URL** `{BASE_URL}/slack/interactions` plus the signing secret in Settings → Slack.
- To use SMS/voice, set the Twilio number's "A message comes in" webhook to `{BASE_URL}/oncall/twilio/sms`.

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

## Historical development notes

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

[0.1.0]: https://github.com/giammbo/incident-commander/releases/tag/v0.1.0
[0.0.4]: https://github.com/giammbo/incident-commander/releases/tag/v0.0.4
[Unreleased]: https://github.com/giammbo/incident-commander/commits/main
