# Incident Commander

Open-source, self-hosted incident management. Declare an incident in one click and — once
configured — it opens a Slack channel and a video bridge, captures a timeline of everything that
happens, tracks roles, follow-ups and custom fields, and gives you a calm place to run the
response. Built to run on your own infrastructure, behind your own SSO.

[![CI](https://github.com/giammbo/incident-commander/actions/workflows/ci.yml/badge.svg)](https://github.com/giammbo/incident-commander/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)

> **Status — production-shaped and running today.** Local auth + RBAC + generic OIDC SSO, the full
> configurable incident lifecycle (severities, statuses, types, roles, timeline, follow-ups, custom
> fields), **postmortems**, **insights/analytics**, **stakeholder updates**, **inbound alerting**
> (AWS CloudWatch/SNS, Prometheus Alertmanager, generic webhook), an **automation/workflow engine**
> (trigger → conditions → actions), a systems/components catalogue with team ownership and a live 3D
> dependency map, and the optional Slack / Google Meet / outgoing-webhook / SMTP integrations are all in place.

## Features

### Incidents

- **One-click declaration** — title, severity, type, public/private, affected system + components, and a **Markdown description**. The chosen **type pre-fills the default severity** and the applicable custom fields.
- **Configurable lifecycle statuses** — define your own workflow (e.g. Triage → Investigating → Identified → Monitoring → Closed); each status carries a category (`triage` / `active` / `closed`) so "is it still open?" logic just works. Reopening is supported.
- **Configurable severities** — your own levels (`SEV1–SEV3`, `P1–P4`, …) with labels, **colours**, and order.
- **Configurable incident types** — Outage / Degraded / Maintenance / Security … each optionally driving a default severity.
- **Roles & assignees** — admin-defined role types (Incident Lead, Communications, Scribe, …); assign one or more people per role on an incident.
- **Timeline** — an automatic chronological log (opened, status change, reopened, closed, role change, Slack/video added) plus **Markdown notes** you can pin.
- **Follow-ups / action items** — create, assign (with a due date), and complete/cancel action items per incident; a global **Follow-ups** page surfaces all open items across incidents.
- **Custom fields** — admin-defined fields (text, long text, single/multi-select, number, checkbox, date), scoped per incident type, shown on the declare form (type-dependent) and the detail.
- **Stakeholder updates** — post a Markdown broadcast (optionally with a status change) that lands on the timeline, posts to the incident's Slack channel, and fires the outgoing webhooks with an `update` event.
- **Postmortems** — a single Markdown document per incident, pre-assembled from the timeline + follow-ups, then editable. If the incident had a Google Meet, the Gemini-generated notes are pulled in automatically (after the call) as a **"Meeting notes (Gemini)"** section — Gemini smart notes and transcription are on by default for every incident Meet (requires the Google Meet API + Gemini for Workspace).

### Insights

- **Dashboard + breakdowns** — totals (open/closed), **MTTR**, open follow-ups, and breakdowns by severity / type / status / system / team over a selectable window (30d / 90d / all), with semantic bar colours.

### Inbound alerting

- **Ingest alerts** from your monitoring into a deduplicated **Alerts inbox** via a token-authenticated endpoint, then promote an alert to a new incident or attach it to an existing one.
- **Adapters** — **AWS SNS** (CloudWatch alarms natively + EventBridge-via-SNS best-effort, with AWS **signature verification**), **Prometheus Alertmanager** (the kube-prometheus webhook), and a **generic JSON** mapper. Alerts dedupe per source key and track a firing↔resolved lifecycle.

### Automation / workflows

- **Trigger → conditions → actions rules** (admin-defined) that run automatically on `incident.opened`, `incident.status_changed`, and `alert.received`. Conditions are AND-matched equalities on a fixed field set (severity / type / privacy / system for incidents; source / severity / status for alerts).
- **Actions** reuse the app's own side-effects: open a Slack channel, assign a role, set status, post a stakeholder update, create a follow-up — and, for inbound alerts, **auto-create an incident** (linking the alert, then running the incident-opened rules). Execution is inline, partial-failure-safe, and recorded on the timeline.

### Catalogue & map

- **Systems & Components** — register the Systems you operate and their Components (each in exactly one System), with a two-tier dependency graph (Component→Component within a system, System→System across systems).
- **Team ownership** — define **Teams** and assign an owning team to each system/component; shown on the catalogue and on the incident detail (who to pull in).
- **3D service map** — an interactive force-graph of systems/components with **live blast-radius**: open incidents light up the affected nodes and everything that depends on them.

### Integrations *(all optional, partial-failure-safe)*

- **Slack** — connect workspaces via OAuth; declaring an incident auto-opens a channel and posts opened/updated/closed messages.
- **Video** — pluggable video providers (Google Meet bridge auto-created per incident) via a small provider abstraction.
- **Outgoing webhooks** — fire on opened/updated/closed/update to **Slack, Microsoft Teams, Discord, or a generic JSON** endpoint.
- **Inbound alerting** — receive alerts from AWS SNS (CloudWatch), Prometheus Alertmanager, or any system via a generic JSON endpoint (see [Inbound alerting](#inbound-alerting) above).

### Platform

- **Self-hosted, one command up** — FastAPI + HTMX from a single container + Postgres, via Docker Compose.
- **Auth & RBAC** — local accounts (argon2) **and** generic **OIDC SSO** (Azure Entra / Okta / Google / any OIDC IdP) with domain gating and a local break-glass. Three group-based roles: Admin, Incident Commander, Read-only.
- **User & group management** — admin UI to invite users (email when SMTP is set, or a generated temp password), organize groups, assign roles.
- **Encrypted settings** — every integration credential/token is encrypted at rest (Fernet); secrets are never rendered in the UI or written to logs.
- **The UI** — a focused dark interface (incident.io-inspired): the **Flamingo** accent marks live incidents and primary actions, severity stays a semantic colour, Markdown is rendered safely (sanitized with `nh3`). Left-sidebar nav: Incidents · Systems · Components · Maps · Follow-ups · Postmortems · Alerts · Insights (+ admin Users / Groups / Automations), with the active page highlighted and Settings/Account at the bottom.

All integrations are partial-failure-safe: an incident is always created as a record, integrations run only when configured and applicable (Slack requires selecting a connection per incident; Google Meet uses the globally-configured service account), and a failing integration is surfaced on the incident — never a 500.

## Quick start (Docker Compose)

Requires Docker.

```bash
git clone https://github.com/giammbo/incident-commander.git
cd incident-commander
cp .env.example .env
```

Generate the two required secrets and put them in `.env`:

```bash
# SESSION_SECRET
python -c "import secrets; print(secrets.token_urlsafe(48))"
# FERNET_KEYS
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then bring it up:

```bash
docker compose up --build
```

Open <http://localhost:8000>. The **bootstrap admin password is printed once in the app logs**
on first start — log in as `IC_ADMIN_EMAIL` (default `admin@localhost`) and change it immediately.

```bash
docker compose logs app | grep "Generated password"
```

## Configuration

Bootstrap secrets live in the environment (`.env`). Everything else — Slack / Google / SMTP / **OIDC SSO**
credentials, and all the catalogues (**severities, statuses, incident types, roles, custom fields, teams,
systems/components**) — is configured at runtime from the admin **Settings** page (secrets stored encrypted
in the database).

| Variable | Purpose | Required | Default |
|---|---|---|---|
| `DATABASE_URL` | Postgres connection (`postgresql+psycopg://…`) | yes | built from `POSTGRES_*` in Compose |
| `SESSION_SECRET` | Signs the session cookie | yes | — (generate it) |
| `FERNET_KEYS` | Comma-separated Fernet keys for encrypting secrets at rest (first encrypts; the rest enable rotation) | yes | — (generate it) |
| `BASE_URL` | Public base URL (used for OAuth/OIDC redirects) | yes | `http://localhost:8000` |
| `IC_ADMIN_EMAIL` | Email of the bootstrap admin | no | `admin@localhost` |
| `SESSION_HTTPS_ONLY` | Mark the session cookie `Secure` — **set `true` in production behind HTTPS** | no | `true` (Compose ships `false` for localhost) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Compose Postgres credentials | no | `ic` / `ic` / `incident_commander` |

> `FERNET_KEY` (singular) is accepted as a convenience alias for a single `FERNET_KEYS` value.

### Single sign-on (OIDC)

Works with any OpenID Connect provider (Azure Entra ID, Okta, Google, Auth0, Keycloak, …). In your IdP,
create an application and register the redirect URI `<BASE_URL>/auth/oidc/callback`. Then in
**Settings → Single sign-on (OIDC)** set the **Issuer URL**, **Client ID**, **Client secret**, an optional
button label, and the **allowed email domains** (empty = trust any user from that issuer); tick *Enable SSO*.
A local-password break-glass remains available so an admin can always sign in. Accounts are matched by the
issuer + subject claim (no silent account takeover).

### Google Meet (video bridge)

Meets are created via the **Google Meet API** under a **service account with Domain-Wide Delegation
(DWD)** that impersonates a fixed Workspace user. No per-user OAuth or Calendar events are involved.

1. In **Google Cloud Console** (<https://console.cloud.google.com>), enable the **Google Meet API**
   and the **Google Drive API**.
2. Create a **service account**; create and download a **JSON key**. No project IAM roles are
   needed. Note its numeric **client ID**.
3. In **Workspace Admin Console → Security → API controls → Domain-wide delegation**, authorize that
   client ID for the scopes `https://www.googleapis.com/auth/meetings.space.created` and
   `https://www.googleapis.com/auth/drive.readonly`.
4. In **Settings → Google**, paste the **JSON key** and an **impersonation user** email — a
   Workspace user with a **Gemini for Workspace** license. Incidents' Meets are created as this
   user, and its Drive receives the Gemini notes. Tick *Enabled*, save.

Incident Meets are created via the Meet API with **Gemini smart notes and transcription on by
default**. No Calendar event is created. Requires a Workspace edition with the Gemini add-on.

(Slack is analogous: create an app at <https://api.slack.com/apps>, register
`<BASE_URL>/connections/slack/callback`, paste the credentials into **Settings → Slack**, then connect a workspace.)

## Development

```bash
uv sync                 # install deps (Python 3.12)
cp .env.example .env     # fill SESSION_SECRET and FERNET_KEYS
uv run pytest            # run the test suite — Docker required (testcontainers spins up Postgres)
uv run ruff check .      # lint
uv run ruff format .     # format
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide.

## Architecture

A single server-rendered FastAPI service: Jinja2 templates with HTMX (no JavaScript build step),
sync SQLAlchemy 2.0 + psycopg3 against Postgres, Alembic migrations applied on startup. Configurable
catalogues (severities, statuses, types, roles, custom fields, teams) follow one consistent pattern;
the timeline auto-captures lifecycle events; integrations (Slack, video providers, outgoing webhooks)
sit behind small abstractions and are partial-failure-safe. Secrets are encrypted at rest with Fernet;
bootstrap secrets come from the environment. Markdown is rendered with `markdown` and sanitized with `nh3`.

## Roadmap

- **Core platform** — local auth + group RBAC, generic OIDC SSO, SMTP email invites ✅
- **Integrations** — Slack channels, Google Meet bridge (via a video-provider abstraction), outgoing webhooks (Slack/Teams/Discord/generic) ✅
- **Incident process** — configurable severities, statuses (with categories), incident types, roles & assignees, timeline (auto events + notes), follow-ups / action items, custom fields, stakeholder updates, postmortems ✅
- **Catalogue** — systems & components with a two-tier dependency graph, team ownership, and a live 3D blast-radius map ✅
- **Insights & alerting** — insights/analytics (MTTR, breakdowns, trends) and inbound alerting (AWS CloudWatch/SNS, Prometheus Alertmanager, generic) into an Alerts inbox with promotion to incidents ✅
- **Automation** — a trigger → conditions → actions workflow engine (incident-opened / status-changed / alert-received), with alert→incident auto-creation ✅
- **Next** — public status page, on-call/paging, a native Slack/Teams bot, and a public API.

## Security

Integration credentials and tokens are encrypted at rest (Fernet), passwords are hashed with
argon2, and sessions use a signed `SameSite=Lax` cookie. State-changing requests are protected
against **CSRF** by an Origin/Referer check (the inbound `/ingest` endpoint is exempt — it is
authenticated by its own secret token). Please report vulnerabilities privately — see
[SECURITY.md](SECURITY.md). Run behind HTTPS with `SESSION_HTTPS_ONLY=true` in production.

**Behind a reverse proxy:** set `BASE_URL` to the public origin **and** forward the original `Host`
header, so the CSRF check's allowed-origin set matches the browser's real origin. For the public
`/ingest` endpoint, also consider a proxy-level rate limit (the app caps each request body at 256 KB).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Dependencies are permissively licensed (MIT / BSD / Apache-2.0), with one exception: the Postgres
driver [`psycopg`](https://www.psycopg.org/) is LGPL-3.0. It is used as an unmodified, separately
installed library, which is compatible with shipping this project under Apache-2.0; its LGPL notice
is retained.
