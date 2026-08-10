#!/usr/bin/env bash
#
# End-to-end check: bootstrap Zammad, wire the webhook, file a ticket, and
# confirm the triage worker routed it.
#
# This is the real path — Zammad's own trigger calls the worker over HTTP.
# Nothing is stubbed.
#
#   ./scripts/smoke.sh
#
# Requires: the stack running (docker compose up -d), curl, python3, and a
# configured AI provider (ANTHROPIC_API_KEY, or a reachable Ollama).

set -euo pipefail

ZAMMAD_URL="${ZAMMAD_URL:-http://localhost:${ZAMMAD_PORT:-8080}}"
WORKER_URL="${WORKER_URL:-http://localhost:${TRIAGE_PORT:-8000}}"
# Where Zammad reaches the worker. Inside the compose network, not localhost.
WORKER_INTERNAL_URL="${WORKER_INTERNAL_URL:-http://triage-worker:8000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-zammad-demo-2026}"
TIMEOUT="${TIMEOUT:-120}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m FAIL\033[0m %s\n' "$*" >&2; exit 1; }

jqp() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null || true; }

# --- 1. wait for Zammad ----------------------------------------------------
log "Waiting for Zammad at $ZAMMAD_URL (up to ${TIMEOUT}s)"
deadline=$(( $(date +%s) + TIMEOUT ))
until curl -fsS "$ZAMMAD_URL/api/v1/getting_started" >/dev/null 2>&1; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "Zammad did not become ready in ${TIMEOUT}s"
  sleep 3
done
ok "Zammad is answering"

# --- 2. run the setup wizard (idempotent) ----------------------------------
setup_done=$(curl -fsS "$ZAMMAD_URL/api/v1/getting_started" | jqp 'd.get("setup_done")')
if [ "$setup_done" != "True" ]; then
  log "Running the auto wizard (creates $ADMIN_EMAIL and the demo groups)"
  curl -fsS "$ZAMMAD_URL/api/v1/getting_started/auto_wizard" >/dev/null \
    || fail "auto wizard failed — is AUTOWIZARD_JSON set in .env?"
  sleep 2
fi
setup_done=$(curl -fsS "$ZAMMAD_URL/api/v1/getting_started" | jqp 'd.get("setup_done")')
[ "$setup_done" = "True" ] || fail "Zammad setup is still incomplete"
ok "Zammad is set up"

AUTH=(-u "$ADMIN_EMAIL:$ADMIN_PASSWORD")

# --- 3. mint an API token --------------------------------------------------
# ticket.agent is everything the worker needs: update a ticket, add tags, add
# an article, read /users/me. It deliberately does NOT include admin — group and
# priority are sent to Zammad by name, so the worker never reads /api/v1/groups.
log "Creating an API token for the worker (ticket.agent only)"
TOKEN=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -X POST "$ZAMMAD_URL/api/v1/user_access_token" \
  -d '{"name":"ai-triage-worker","permission":["ticket.agent"]}' | jqp 'd["token"]')
[ -n "$TOKEN" ] || fail "could not create an API token (check the admin password)"
ok "token created"

if grep -q '^ZAMMAD_TOKEN=' .env 2>/dev/null; then
  # Keep the token out of the shell history and out of `ps`.
  python3 - "$TOKEN" <<'PY'
import pathlib, sys
token = sys.argv[1]
path = pathlib.Path(".env")
lines = path.read_text().splitlines()
path.write_text(
    "\n".join(f"ZAMMAD_TOKEN={token}" if ln.startswith("ZAMMAD_TOKEN=") else ln for ln in lines)
    + "\n"
)
PY
  log "Wrote ZAMMAD_TOKEN to .env and restarting the worker"
  docker compose up -d triage-worker >/dev/null
  sleep 5
fi

deadline=$(( $(date +%s) + 60 ))
until [ "$(curl -fsS "$WORKER_URL/healthz" | jqp 'd["zammad"]')" = "True" ]; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "worker never connected to Zammad"
  sleep 2
done
ok "worker is authenticated against Zammad"

# --- 4. wire the webhook + trigger ----------------------------------------
log "Creating the Zammad webhook and trigger"
HOOK_NAME="AI triage worker"
TRIGGER_NAME="AI triage on ticket create"

# Idempotent: re-running the smoke test must not pile up duplicates.
find_by_name() {
  curl -fsS "${AUTH[@]}" "$ZAMMAD_URL/api/v1/$1" \
    | python3 -c "import sys,json;print(next((str(r['id']) for r in json.load(sys.stdin) if r.get('name')==sys.argv[1]),''))" "$2" 2>/dev/null || true
}

HOOK_ID=$(find_by_name webhooks "$HOOK_NAME")
if [ -z "$HOOK_ID" ]; then
  HOOK_ID=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST "$ZAMMAD_URL/api/v1/webhooks" -d "$(cat <<JSON
{
  "name": "$HOOK_NAME",
  "endpoint": "$WORKER_INTERNAL_URL/webhook/zammad",
  "note": "Created by scripts/smoke.sh",
  "active": true
}
JSON
)" | jqp 'd["id"]')
fi
[ -n "$HOOK_ID" ] || fail "could not create the webhook"
ok "webhook id=$HOOK_ID -> $WORKER_INTERNAL_URL/webhook/zammad"

TRIGGER_ID=$(find_by_name triggers "$TRIGGER_NAME")
if [ -z "$TRIGGER_ID" ]; then
  TRIGGER_ID=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST "$ZAMMAD_URL/api/v1/triggers" -d "$(cat <<JSON
{
  "name": "$TRIGGER_NAME",
  "condition": { "ticket.action": { "operator": "is", "value": "create" } },
  "perform": { "notification.webhook": { "webhook_id": "$HOOK_ID" } },
  "activator": "action",
  "execution_condition_mode": "selective",
  "active": true
}
JSON
)" | jqp 'd["id"]')
fi
[ -n "$TRIGGER_ID" ] || fail "could not create the trigger"
ok "trigger id=$TRIGGER_ID"

# --- 5. file a ticket ------------------------------------------------------
log "Creating a test ticket"
TICKET=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -X POST "$ZAMMAD_URL/api/v1/tickets" -d "$(cat <<'JSON'
{
  "title": "VPN drops every few minutes for the whole Berlin office",
  "group": "Users",
  "priority": "2 normal",
  "customer_id": "guess:dana@example.org",
  "article": {
    "subject": "VPN drops every few minutes",
    "body": "Since this morning nobody in the Berlin office can stay connected to the VPN. It disconnects roughly every five minutes. Around 20 people are affected and none of us can reach the file server.",
    "type": "note",
    "internal": false
  }
}
JSON
)")
TICKET_ID=$(echo "$TICKET" | jqp 'd["id"]')
[ -n "$TICKET_ID" ] || fail "could not create a ticket: $TICKET"
# What it looked like before triage, so we can prove the worker changed it.
BEFORE_GROUP=$(echo "$TICKET" | jqp 'd["group_id"]')
BEFORE_PRIORITY=$(echo "$TICKET" | jqp 'd["priority_id"]')
ok "ticket id=$TICKET_ID created (group_id=$BEFORE_GROUP priority_id=$BEFORE_PRIORITY)"

# --- 6. wait for the worker to route it ------------------------------------
# Note: the plain ticket endpoint, *not* ?all=true — the latter returns an
# assets-wrapped payload with no top-level group_id.
log "Waiting up to 30s for triage"
deadline=$(( $(date +%s) + 30 ))
while :; do
  CURRENT=$(curl -fsS "${AUTH[@]}" "$ZAMMAD_URL/api/v1/tickets/$TICKET_ID")
  GROUP_ID=$(echo "$CURRENT" | jqp 'd.get("group_id","")')
  PRIORITY_ID=$(echo "$CURRENT" | jqp 'd.get("priority_id","")')
  [ -n "$GROUP_ID" ] || fail "could not read ticket $TICKET_ID back: $CURRENT"
  if [ "$GROUP_ID" != "$BEFORE_GROUP" ] || [ "$PRIORITY_ID" != "$BEFORE_PRIORITY" ]; then
    break
  fi
  [ "$(date +%s)" -lt "$deadline" ] || break
  sleep 2
done

GROUP_NAME=$(curl -fsS "${AUTH[@]}" "$ZAMMAD_URL/api/v1/groups/$GROUP_ID" | jqp 'd["name"]')
PRIORITY_NAME=$(curl -fsS "${AUTH[@]}" "$ZAMMAD_URL/api/v1/ticket_priorities/$PRIORITY_ID" | jqp 'd["name"]')
TAGS=$(curl -fsS "${AUTH[@]}" "$ZAMMAD_URL/api/v1/tags?object=Ticket&o_id=$TICKET_ID" | jqp '",".join(d.get("tags",[]))')

echo
echo "  ticket   : $ZAMMAD_URL/#ticket/zoom/$TICKET_ID"
echo "  group    : ${GROUP_NAME:-?} (was group_id=$BEFORE_GROUP)"
echo "  priority : ${PRIORITY_NAME:-?} (was priority_id=$BEFORE_PRIORITY)"
echo "  tags     : ${TAGS:-(none)}"
echo

[ -n "$GROUP_NAME" ] && [ -n "$PRIORITY_NAME" ] || fail "could not resolve the ticket's group/priority names"

if [ "$GROUP_ID" = "$BEFORE_GROUP" ] && [ "$PRIORITY_ID" = "$BEFORE_PRIORITY" ] && [ -z "$TAGS" ]; then
  echo "  worker log:"
  docker compose logs --tail 20 triage-worker 2>&1 | sed 's/^/    /'
  fail "the ticket was not modified — see the worker log above"
fi

ok "SMOKE TEST PASSED — the worker triaged the ticket"
