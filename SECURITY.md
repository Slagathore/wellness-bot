# Security

Mira is a self-hosted wellness bot. You run the whole stack (bot, admin panel, database,
background workers) yourself, on hardware you control. This document is the honest
version of what happens to your data and what could go wrong.

## Reporting a vulnerability

Open a private security advisory on GitHub: [Slagathore/wellness-bot > Security > Report
a vulnerability](https://github.com/Slagathore/wellness-bot/security/advisories/new). If
it's not sensitive, a regular [issue](https://github.com/Slagathore/wellness-bot/issues)
is fine too. This is a side project maintained by one person, so there's no SLA, but real
reports get read and acted on.

## Threat model

This app assumes you're running it on hardware you own or fully trust, for yourself or
people you know personally.

What a stranger on your LAN could reach: nothing, as long as the admin panel stays on
`127.0.0.1` (the default) and you don't port forward it. If you bind it to `0.0.0.0` or
put it behind a reverse proxy with no auth in front, they get whatever `ADMIN_USERNAME` /
`ADMIN_PASSWORD` normally gates: user data, broadcast tools, and, if you've also turned on
`ENABLE_DANGEROUS_TOOLS`, raw SQL and file write access (see "Admin panel" below).

What someone with local filesystem access on the host could reach: `.env` (bot token,
admin password, any API keys), the SQLite database, and `wellness_data/`, all
unencrypted. Local file access on this host is effectively full access to everything the
bot has ever stored, same as most self-hosted single user apps.

The main outside relationship day to day is Telegram's servers, which your messages
transit to reach the bot, and, if you leave the model defaults as they are, Ollama's
cloud service for a couple of specific jobs. See the next section.

## What leaves your machine, and when

Everything routes through [Ollama](https://ollama.com) at `OLLAMA_HOST` (local by
default). Ollama decides whether to run a model locally or forward it to Ollama's hosted
cloud, based on the model tag. A plain local model tag (`llama3:latest`, `llava:latest`,
whatever you've pulled) never leaves `OLLAMA_HOST`. A model tag ending in `:cloud` gets
forwarded by Ollama itself.

Six settings pick the model per role: `CHAT_MODEL`, `WORKER_MODEL`, `VISION_MODEL`,
`PLANNER_MODEL`, `NIGHTLY_MODEL`, `EMBED_MODEL`. `CHAT_MODEL`, `WORKER_MODEL`, and
`EMBED_MODEL` default to a local model. **`VISION_MODEL` and `PLANNER_MODEL` default to a
cloud model (`mistral-large-3:675b-cloud`), and `NIGHTLY_MODEL` defaults to a cloud model
(`kimi-k2.7-code:cloud`).** That's a deliberate tradeoff, cloud models profile and plan
noticeably better than what most people can run locally, not an oversight, but it means
real conversation content leaves the machine by default in two places:

- **The nightly profiling job** (`app/workers/nightly.py`, installed as
  `systemd/telegram-nightly.service`). Once a user has sent 20 or more messages, this job
  takes their most recent messages, up to 50, and sends them to `NIGHTLY_MODEL` to build a
  psychological profile (mental health indicators, Big Five personality scores) that gets
  stored in the database. Runs automatically every night for every active user. No
  feature flag gates it. This is the most sensitive data category in the app.
- **The sentiment worker** (`app/workers/sentiments.py`, installed as
  `systemd/telegram-sentiments.service`). Runs continuously, picks up every new user
  message, and sends its content to `PLANNER_MODEL` for scoring, usually within a few
  seconds of the message being sent. `PLANNER_MODEL` also backs the turn planner, which is
  off by default behind a feature flag.

Set `VISION_MODEL`, `PLANNER_MODEL`, and `NIGHTLY_MODEL` to a local Ollama model in
`.env` (see the commented examples in `.env.example`) to keep all of this on your
machine. Nothing else in the app calls out to a cloud service.

If you ever set a model string with an `openai/`, `anthropic/`, `google/`, `cohere/`, or
`mistral/` prefix (see `docs/cloud_models.md`), that's a separate path: it calls that
provider's API directly with a key you supply, bypassing Ollama entirely.

## PII and mental health data

`wellness_data/` and the SQLite database (`DATABASE_PATH`) hold real user data:
conversation history, sentiment scores, reminders, and the psychological profiles
described above. Both are gitignored and excluded from Docker image builds
(`.dockerignore`). Neither is encrypted at rest; anyone with read access to the host
filesystem can read them straight out of SQLite or the JSON files under `wellness_data/`.
If that matters for your setup, put the data directory on an encrypted volume.

## Admin panel

- The admin panel binds to `127.0.0.1` by default (`app/interfaces/admin/server.py`).
  Don't change that to `0.0.0.0` without putting a TLS reverse proxy in front of it; the
  login is HTTP Basic over whatever transport you give it.
- Login is `ADMIN_USERNAME` / `ADMIN_PASSWORD`, checked as plaintext HTTP Basic auth.
  This is not bcrypt, despite `bcrypt` being listed in `requirements.txt` (that dependency
  isn't actually imported anywhere in the code today). The separate desktop Tk control
  panel uses its own salted SHA-256 hash (`AdminAuth` in `app/utils/security.py`), also
  not bcrypt. Keep `.env` secret and treat the admin password like any other credential.
- `ENABLE_DANGEROUS_TOOLS`, `ADMIN_DB_EDIT_ENABLED`, `ADMIN_LLM_CONSOLE_ENABLED`, and
  `ADMIN_OMNI_BROADCAST_ENABLED` all default to `false`. Turning `ENABLE_DANGEROUS_TOOLS`
  on by itself does nothing further; each tool also needs its own flag set. Once both are
  on, the LLM console and DB edit tools can run real SQL and write files under the project
  root. Only turn these on somewhere you fully control, and never on an instance reachable
  from the internet.

## Known limitations

- No encryption at rest for the database or `wellness_data/`.
- Admin web login is plaintext HTTP Basic, not bcrypt (see "Admin panel" above).
- `VISION_MODEL` and `PLANNER_MODEL` default to a cloud model, and so does
  `NIGHTLY_MODEL`; see "What leaves your machine, and when" above.
- This is a hobby project maintained by one person. It hasn't had a professional
  security audit. Treat it accordingly if you're handling data more sensitive than your
  own.
