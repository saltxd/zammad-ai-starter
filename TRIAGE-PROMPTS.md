# The triage prompt

The prompt lives in [`triage-worker/triage_worker/classify.py`](triage-worker/triage_worker/classify.py)
as `SYSTEM_PROMPT`, rendered per-request with your queues, priorities, and tags.
This document explains why it says what it says, so you can change it without
re-learning the same lessons.

## What the model sees

**System turn** — the rendered prompt: the role, your taxonomy, and the
guidance below.

**User turn** — the ticket, and nothing else:

```
Subject: VPN drops every few minutes
From: dana@example.org

Since this morning the VPN disconnects roughly every 5 minutes.
```

No conversation history, no prior tickets, no agent notes. One ticket, one
classification, one call. That keeps the cost per ticket flat and predictable,
and it means a bad classification can never poison the next one.

The worker also refuses to send its own output back in. An internal article
(the note it just wrote, or any agent note) is acknowledged and dropped
before the model is called. Combined with the `triaged` stamp, a trigger that
fires on ticket update cannot turn into a loop of the model classifying its
own rationale.

## The response contract

```json
{
  "group": "2nd Level",
  "priority": "3 high",
  "tags": ["network"],
  "reason": "Multiple users on one site affected; needs network diagnostics.",
  "confidence": 0.86
}
```

The schema is generated from your configured taxonomy and sent to the provider
as a JSON Schema, so `group` and `priority` are constrained to values that
actually exist. Constraining the model is cheaper than repairing its output.

`parse_classification()` re-validates everything anyway. Not defensiveness for
its own sake: schema enforcement varies by provider and by model, the local
Ollama path is best-effort, and the parser is what makes the whole thing
testable without a network. See `tests/test_classify.py` for the cases it
handles — fenced JSON, prose around the JSON, a queue that doesn't exist, a
confidence of `85` instead of `0.85`.

## The four instructions that carry their weight

Everything else in the prompt is taxonomy. These four lines are the ones that
changed behaviour in testing.

### 1. Urgency is business impact, not tone

> Judge urgency by business impact, not by the customer's tone. An angry
> message about a cosmetic issue is still low priority; a calm message saying
> "nobody in the warehouse can log in" is high.

Without this, models track sentiment. Capital letters and "URGENT!!!" in the
subject reliably produce high priority for a typo on a settings page, while a
politely-worded outage report lands as normal. This is the single highest-value
line in the prompt, and it is the one to make more specific for your business:
if a down POS terminal outranks a down office printer, say so explicitly.

### 2. Prefer the default queue over guessing

> If several queues could fit, choose the one that can take the FIRST action.
> Prefer the default queue "Users" over guessing. A ticket in the wrong
> specialist queue is more expensive than one in the general queue.

Misrouting is asymmetric. A ticket sitting in the general queue gets picked up
in minutes by whoever is on duty. A ticket sitting in the wrong specialist queue
is invisible — the people watching that queue don't recognise it, the people who
should have it never see it, and it surfaces when someone asks why nobody
replied. Telling the model this up front is worth more than any amount of
routing detail.

"The queue that can take the first action" resolves the common ambiguity where
a ticket is *about* billing but *blocked on* a login problem. Access first.

### 3. Confidence is a real number, not a formality

> Set confidence honestly. Below 0.60 the ticket is left untouched for a human,
> which is the correct outcome when the ticket is vague, is in a language you
> are unsure of, or could plausibly go to two queues. Do not inflate confidence
> to seem useful.

Confidence only means something if a low number has a consequence, and if the
model knows what that consequence is. Naming the threshold and the outcome —
*a human handles it* — makes abstention a legitimate answer rather than a
failure. Without the last sentence, models cluster at 0.9 regardless.

That number is enforced in code, not trusted from the model:
`Classification.is_confident()` gates every write. Start at `0.6`; watch what
gets skipped for a week before lowering it.

### 4. The reason is for the agent, not for you

> reason: one sentence, for the agent who picks this up. Say what drove the
> routing, not what the ticket said.

Otherwise the model summarises the ticket back — which the agent can already
read. What the agent can't see is *why it landed in their queue*, which is the
only thing that makes the note worth the pixels. It also makes the log readable
when you're auditing a week of routing decisions.

## Tuning it for your helpdesk

**Start in dry-run.** `TRIAGE_DRY_RUN=true` classifies and logs without writing.
Point it at real inbound tickets for a few days and read the log. You will find
that a third of your queue names mean something to you that they don't mean to a
model reading them cold.

**Describe your queues.** The biggest single improvement is replacing bare names
with one-line descriptions. `TRIAGE_GROUPS` takes names because those must match
Zammad exactly, so add the descriptions to the prompt itself:

```
Available queues (choose exactly one):
- Users — general first-line support; anything not clearly below
- 2nd Level — infrastructure, network, VPN, server-side faults
- Billing — invoices, payment failures, plan and licence changes
```

**Add the cases you actually argue about.** Every helpdesk has three or four
recurring judgment calls its agents have internalised. Those are worth stating:

```
- A password reset for a single user is "Users" at "1 low", even when the
  customer says it is urgent.
- Anything affecting more than one person at the same site is at least
  "2 normal" and usually belongs in "2nd Level".
- Suspected phishing or account compromise is always "3 high" and tagged
  "security", regardless of how it is phrased.
```

**Don't shout.** Current models follow the system prompt closely. Prompts
written to overcome older models' reluctance — `CRITICAL:`, `You MUST ALWAYS`,
`If in doubt, use X` — now over-trigger. Plain declarative sentences work
better, and every emphatic line makes the next one mean less.

**Change one thing at a time.** Keep ten or twenty real tickets with the routing
you'd have chosen by hand, and re-run them after every prompt edit. A prompt
change that fixes the case in front of you and quietly breaks two others is the
normal failure mode here, and it is invisible without a fixed set to check
against.

## Cost

One classification is roughly 400–600 input tokens (prompt plus ticket) and
under 100 output tokens. The taxonomy is identical on every request, so if you
run enough volume for it to matter, the system prompt is a natural prompt-cache
breakpoint.

`ANTHROPIC_EFFORT=low` is the default because triage is a short classification
rather than a reasoning problem, and low effort is where the cost and latency
win is. Raise it to `medium` if you see routing mistakes on genuinely ambiguous
tickets — but read the dry-run log first. Most routing mistakes are a prompt
that never explained what the queues are for.
