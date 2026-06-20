# Security Policy

Incident Commander stores sensitive material — OAuth tokens, integration secrets, and user
credentials — so we take security seriously. Thanks for helping keep it and its users safe.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub Security Advisories:
<https://github.com/giammbo/incident-commander/security/advisories/new>

Please include a description, reproduction steps, affected version/commit, and impact. We'll
acknowledge your report, work with you on a fix, and credit you (if you'd like) when it ships.

## Supported versions

The project is pre-1.0. Only the latest `main` is supported; fixes land there and there are no
backports yet. Pin to a commit you've reviewed if you run it in production.

## Security model

- **Secrets at rest** are encrypted with Fernet (`MultiFernet`, so keys can be rotated). Only secret
  values are encrypted; lookup keys (e.g. workspace/account identifiers) stay plaintext so they can be indexed.
- **Passwords** are hashed with **argon2**; plaintext passwords are never stored or logged.
- **Sessions** use a signed cookie (`SameSite=Lax`) holding only the user id; the user and roles are
  loaded per request. The `Secure` flag is controlled by `SESSION_HTTPS_ONLY`.
- **Bootstrap secrets** (`SESSION_SECRET`, `FERNET_KEYS`, DB URL) come from the environment, never the database.
- **Access control** is group-based RBAC (Admin / Incident Commander / Read-only); the bootstrap
  admin is protected (cannot be deactivated, demoted, or removed from the Admins group).

## Deployment hardening

- **Run behind HTTPS** and set `SESSION_HTTPS_ONLY=true` (the code default). Compose ships it as
  `false` only so the app works over plain HTTP on localhost.
- **Use a reverse proxy** that terminates TLS and sets forwarded headers; the app runs uvicorn with
  `--proxy-headers` so it derives the correct scheme/host.
- **Protect `FERNET_KEYS`.** Losing it makes every stored secret unrecoverable. Back it up out of band.
  To rotate, prepend a new key (encryption uses the first key; older keys still decrypt), then re-encrypt.
- Use a strong, unique `SESSION_SECRET` and a strong Postgres password.

## Known considerations

- **Admin temp-password in a URL (Phase 1).** When an admin creates a user, the generated temporary
  password is currently passed in a redirect query string, so it can appear in access logs and the
  `Referer` header. It's only reachable by an authenticated admin. This will be replaced by SMTP
  email invites in Phase 2. Until then, treat your app logs as sensitive.
- **Google OAuth refresh tokens (Phase 4).** When the Google integration ships, the OAuth consent
  screen must be published "In production" — in "Testing" status Google revokes refresh tokens after 7 days.
- **Database driver license.** The Postgres driver `psycopg` is LGPL-3.0 (used unmodified as a
  library). This is a licensing note, not a vulnerability.

## Security updates

Dependencies are pinned in `uv.lock`; run `uv run pytest` after upgrades. We recommend watching the
repository and keeping your deployment up to date with `main`.
