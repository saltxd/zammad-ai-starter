# AI-Native Zammad starter

A working [Zammad](https://zammad.org) helpdesk plus an AI triage worker, in one
`docker compose up`. A ticket arrives, a model reads it, and the ticket lands in
the right queue at the right priority with the right tags — or is left alone for
a human, which is the point.

This is a **teaching repo**. It is small enough to read in one sitting, the
interesting logic is pure and tested, and every design decision that cost me an
afternoon is written down next to the code that caused it.

```
  Zammad  ──trigger──▶  POST /webhook/zammad
                              │
                              ├─ parse the ticket out of the payload   (pure)
                              ├─ classify it                (Claude, or Ollama,
                              │                              or keyword rules)
                              ├─ confident enough?  no ─────▶ leave it for a human
                              │                     yes
                              └─ write back group + priority + tags + a note
                                        │
  Zammad  ◀──────REST API───────────────┘
```

## Quickstart

```bash
git clone https://github.com/saltxd/zammad-ai-starter.git
cd zammad-ai-starter
cp .env.example .env
$EDITOR .env          # add ANTHROPIC_API_KEY, or point OLLAMA_URL at a local model
docker compose up -d
./scripts/smoke.sh
```

`smoke.sh` waits for Zammad, runs its setup wizard, mints an API token for the
worker, creates the webhook and trigger, files a test ticket, and checks that it
got routed. It is the acceptance test for this repo, and it exercises the real
path — Zammad's own trigger calling the worker over HTTP, nothing stubbed.

Then:

- **Zammad** — <http://localhost:8080> (`admin@example.com` / `zammad-demo-2026`)
- **Worker** — <http://localhost:8000/healthz>, API docs at `/docs`

First start pulls ~1.5 GB of images and runs database migrations; budget a few
minutes. Everything after that is seconds.

> **This is a demo configuration.** It ships with a known admin password, no TLS,
> and webhook signature verification switched off. Read [Going to
> production](#going-to-production) before pointing it at anything real.

## Wiring it into an existing Zammad

Two objects, both under **Manage** in the admin panel — or see `scripts/smoke.sh`
for the same thing via the API.

**1. Manage → Webhook → New Webhook**

| Field | Value |
|---|---|
| Endpoint | `http://triage-worker:8000/webhook/zammad` |
| HMAC token | a random string — put the same value in `ZAMMAD_WEBHOOK_SECRET` |

The endpoint is whatever hostname Zammad can reach the worker on. Inside this
compose stack that's `triage-worker`; elsewhere it's your host and port.

**2. Manage → Trigger → New Trigger**

| Field | Value |
|---|---|
| Conditions | Ticket → Action → is → created |
| Action | Notification → Webhook → *(the webhook above)* |

Then set `TRIAGE_GROUPS` in `.env` to the group names that exist in *your*
Zammad, exactly as spelled there. Group and priority are sent to Zammad
by name; it rejects an unknown one with `422 No lookup value found`, so a typo
surfaces on the first ticket instead of silently doing nothing. That also keeps
the worker's API token on `ticket.agent` — reading `/api/v1/groups` to resolve
names client-side would require an admin-scoped token.

## Configuration

Everything is environment variables; see [`.env.example`](.env.example) for the
annotated list. The ones that matter:

| Variable | Default | What it does |
|---|---|---|
| `AI_PROVIDER` | `anthropic,ollama` | Providers to try, in order. First one that answers wins. |
| `ANTHROPIC_API_KEY` | — | Blank skips the Claude provider entirely. |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Any current Claude model. |
| `ANTHROPIC_EFFORT` | `low` | Triage is a short classification; low is the cost/latency sweet spot. |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | An Ollama server on the Docker host. |
| `TRIAGE_GROUPS` | `Users,2nd Level,Billing` | Must match Zammad's group names exactly. |
| `TRIAGE_CONFIDENCE_THRESHOLD` | `0.6` | Below this, the ticket is left untouched. |
| `TRIAGE_DRY_RUN` | `false` | Classify and log, write nothing. **Start here.** |
| `ZAMMAD_WEBHOOK_SECRET` | — | Blank disables signature verification. |

### Run it in dry-run first

```bash
sed -i '' 's/^TRIAGE_DRY_RUN=.*/TRIAGE_DRY_RUN=true/' .env
docker compose up -d triage-worker
docker compose logs -f triage-worker
```

Every ticket gets classified and logged; nothing is written to Zammad. Leave it
like that against real inbound mail for a few days and read the log. You will
learn more about your own queue names than about the model.

## How it works

| File | What's in it |
|---|---|
| [`webhook.py`](triage-worker/triage_worker/webhook.py) | Pull the ticket out of Zammad's payload. Pure, tolerant of shape differences. |
| [`classify.py`](triage-worker/triage_worker/classify.py) | The prompt, the JSON schema, the response parser, the keyword fallback. Pure. |
| [`providers.py`](triage-worker/triage_worker/providers.py) | Claude and Ollama behind one three-line protocol, plus the fallback chain. |
| [`zammad.py`](triage-worker/triage_worker/zammad.py) | Three REST calls: update the ticket, tag it, add the note. |
| [`main.py`](triage-worker/triage_worker/main.py) | FastAPI wiring, signature verification, and the two safety policies. |

The prompt and the reasoning behind each line of it are in
[TRIAGE-PROMPTS.md](TRIAGE-PROMPTS.md).

### The two rules that make it safe to leave running

**Never write a low-confidence classification.** The model is told the threshold
and told what happens below it — a human handles the ticket. Abstaining is a
legitimate answer, and `Classification.is_confident()` enforces it in code
rather than trusting the number.

**Never return 5xx to a webhook.** Zammad retries failures. A provider outage
that turns into a retry storm is how one bad afternoon becomes an incident. A
model failure falls back to keyword rules (which report zero confidence, so they
never write); a Zammad write failure is logged and acknowledged.

### Tests

No network, no API key:

```bash
cd triage-worker
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest
```

All 78 cover cases that actually happen — fenced JSON, a chatty preamble around
the JSON, a queue the model invented, a confidence of `85` instead of `0.85`,
tags as a comma-string, an HTML article body, a webhook payload we can't parse,
a signature computed over re-serialised JSON. That last one is why
`verify_signature()` takes raw bytes.

## Going to production

Things this repo does **not** do for you:

- **Change the admin password.** `AUTOWIZARD_JSON` in `.env.example` creates
  `admin@example.com` with a published password. Delete that line, or change it,
  before Zammad is reachable by anyone else.
- **Set `ZAMMAD_WEBHOOK_SECRET`.** Blank means any host that can reach the worker
  can drive it. Set it here and in the Zammad webhook's HMAC token field.
- **Terminate TLS.** Put both services behind a reverse proxy you trust.
- **Add Elasticsearch.** `ELASTICSEARCH_ENABLED=false` means Zammad searches the
  database. Fine for a demo, slow for a real queue.
- **Back up Postgres.** The compose file keeps data in named volumes and takes
  no backups.

## Where this came from

I run a self-hosted helpdesk and got tired of reading tickets just to decide who
should read them. The interesting part was never the API call — it was
everything around it: what the model does when the ticket is ambiguous, what
happens when the API is down at 2am, and how you keep a confidence score from
becoming decoration. That's what this repo is about.

If that's useful, the extended version — deployment automation, the middleware
reference, and the operational runbook — is the paid kit. Details and the
occasional writeup on running AI against infrastructure you own:
**<https://github.com/saltxd>**.

## License

MIT — see [LICENSE](LICENSE). Zammad itself is AGPL-3.0 and is used here as an
unmodified upstream container image.
