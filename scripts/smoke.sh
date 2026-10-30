#!/usr/bin/env bash
# End-to-end smoke test: boots the REAL docker-compose stack (no testcontainers,
# no mocks) and exercises login -> declare-incident over HTTP, so it catches
# deploy-path bugs (missing DB commit, migration/startup ordering, hook wiring)
# that unit tests never touch.
set -euo pipefail

# Always operate from the repo root, regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PROJECT="ic-smoke"
PORT="${SMOKE_PORT:-18000}"
BASE="http://localhost:${PORT}"

# secrets — pure-stdlib, generated fresh every run, never hardcoded/committed.
export APP_PORT="$PORT"
export BASE_URL="$BASE"           # required so the CSRF Origin/Referer check accepts our requests
export SESSION_HTTPS_ONLY="false" # curl over plain http must receive the session cookie
SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export SESSION_SECRET
FERNET_KEYS="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
export FERNET_KEYS

JAR="$(mktemp)"

compose() { docker compose -p "$PROJECT" "$@"; }
cleanup() {
  rm -f "$JAR"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Issue a request, return only the HTTP status code (never fails on non-2xx
# so callers can assert the expected code themselves — logins/declares 303).
req() {
  curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -b "$JAR" \
    -H "Origin: $BASE" -H "Referer: $BASE/" "$@"
}

fail() {
  echo "SMOKE-FAIL: $1"
  echo "---- app logs (tail) ----"
  compose logs app 2>&1 | tail -80
  exit 1
}

echo "== bring up the stack (project=$PROJECT port=$PORT) =="
compose up -d --build

echo "== wait for /healthz =="
up=""
for i in $(seq 1 90); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
    up=1
    break
  fi
  sleep 2
done
[ -n "$up" ] || fail "healthz never came up"

echo "== scrape bootstrap admin password from logs =="
PW="$(compose logs app 2>&1 | grep -oE 'Generated password \(shown once\): [^[:space:]]+' | tail -1 | awk '{print $NF}')"
[ -n "$PW" ] || fail "no bootstrap admin password found in app logs"

echo "== login =="
code="$(req -X POST --data-urlencode "email=admin@localhost" --data-urlencode "password=$PW" "$BASE/login")"
[ "$code" = "303" ] || fail "login expected 303, got $code"

echo "== clear forced password change (bootstrap admin has must_change_password) =="
code="$(req -X POST --data-urlencode "name=Smoke Admin" --data-urlencode "new_password=Smoke-123456" --data-urlencode "confirm=Smoke-123456" "$BASE/account/password")"
[ "$code" = "303" ] || fail "password change expected 303, got $code"

echo "== declare an incident (title only; severity is optional) =="
code="$(req -X POST --data-urlencode "title=Smoke test incident" "$BASE/incidents")"
[ "$code" = "303" ] || fail "declare incident expected 303, got $code"

echo "== assert it shows on the incidents list =="
curl -fsS -c "$JAR" -b "$JAR" "$BASE/" | grep -q "Smoke test incident" \
  || fail "incident not visible on incidents list"

# POST and print the Location header of the redirect (empty if none).
post_loc() {
  curl -s -o /dev/null -D - -c "$JAR" -b "$JAR" -H "Origin: $BASE" -H "Referer: $BASE/" \
    -X POST "$@" | tr -d '\r' | awk 'tolower($1)=="location:" {print $2}'
}

echo "== on-call: schedule + rotation + escalation path =="
SCHED="$(post_loc --data-urlencode "name=Smoke primary" --data-urlencode "timezone=UTC" "$BASE/oncall/schedules")"
[ -n "$SCHED" ] || fail "schedule create gave no redirect"
code="$(req -X POST --data-urlencode "name=Daily" --data-urlencode "user_ids=1" \
  --data-urlencode "shift_hours=24" --data-urlencode "handoff_local=09:00" \
  --data-urlencode "start_date=$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d yesterday +%Y-%m-%d)" "$BASE$SCHED/rotations")"
[ "$code" = "303" ] || fail "rotation create expected 303, got $code"
PATH_URL="$(post_loc --data-urlencode "name=Smoke path" "$BASE/oncall/escalation-paths")"
[ -n "$PATH_URL" ] || fail "path create gave no redirect"
code="$(req -X POST --data-urlencode "schedule_ids=${SCHED##*/}" --data-urlencode "ack_timeout_minutes=5" \
  "$BASE$PATH_URL/levels")"
[ "$code" = "303" ] || fail "level create expected 303, got $code"

echo "== on-call: manual page to the path =="
ESC="$(post_loc --data-urlencode "target_kind=path" --data-urlencode "path_id=${PATH_URL##*/}" \
  --data-urlencode "urgency=high" --data-urlencode "message=smoke page" "$BASE/oncall/page")"
case "$ESC" in /oncall/escalations/*) ;; *) fail "manual page gave no escalation redirect ($ESC)" ;; esac

echo "== on-call: wait for the worker tick to process the page =="
# SMTP is not configured in the smoke stack, so email is unreachable: the engine records a
# "level_empty ... no configured channel" event (a configured SMTP would show an email row
# with status sent/skipped instead). Either proves the worker picked the page up and ran.
ok=""
for i in $(seq 1 30); do
  page="$(curl -fsS -c "$JAR" -b "$JAR" "$BASE$ESC" || true)"
  flat="$(printf '%s' "$page" | tr -d '\n')"
  if printf '%s' "$flat" | grep -qE 'level_empty|<td>email</td>[[:space:]]*<td><span class="chip[^"]*">(sent|skipped)</span>'; then
    ok=1
    break
  fi
  sleep 2
done
[ -n "$ok" ] || fail "escalation $ESC was never processed by the worker (no email row, no level_empty event)"
printf '%s' "$flat" | grep -q 'no configured channel' || printf '%s' "$flat" | grep -q '<td>email</td>' \
  || fail "escalation $ESC processed but without the expected email outcome"

echo "SMOKE-OK"
