# Mira — Wellness Bot

A self-hosted, multi-platform AI wellness companion built on Telegram (and optionally Discord). Mira runs entirely on your own infrastructure using local LLMs via [Ollama](https://ollama.com), with no mandatory cloud dependencies.

---

## Features

- **Conversational wellness support** — context-aware chat with persistent memory across sessions
- **Personality modes** — Professional, Friendly, Creative, Therapeutic, Work Focus, Roleplay, and more; switchable per-user
- **Custom characters** — import or create custom AI personas stored in the database
- **Psychological profiling** — passive sentiment tracking and psych-profile generation from conversations
- **Proactive reminders** — the bot sets and delivers follow-up reminders based on conversation context
- **Nightly analytics** — background worker summarizes daily sentiment trends
- **Semantic memory (RAG)** — past conversations are embedded and retrieved for long-term context
- **Image generation** — optional local or remote image backends (FLUX.2 Klein, EasyDiffusion, Perchance)
- **Admin web GUI** — FastAPI-powered moderation panel with user management, broadcast, and LLM console
- **Discord integration** — optional Discord bot running alongside Telegram
- **Adaptive psych tests** — in-chat personality assessments that refine the user profile

---

## Architecture

```text
┌─────────────────────────────────────────────────────┐
│                   Telegram / Discord                │
│                  (python-telegram-bot / discord.py)  │
└──────────────────────┬──────────────────────────────┘
                       │ updates
                       ▼
┌──────────────────────────────────────────────────────┐
│              Orchestrator / Pipeline                 │
│   (app/orchestrator/)  —  context builder, prompt    │
│   builder, persona runtime, pipeline dispatch        │
└──────┬─────────────────────────────────┬────────────┘
       │                                 │
       ▼                                 ▼
┌─────────────┐                  ┌───────────────────┐
│   Ollama    │                  │   SQLite (WAL)    │
│  (LLM/emb) │                  │  + sqlite-vec     │
└─────────────┘                  │  (conversation,   │
                                 │   profiles, RAG)  │
                                 └───────────────────┘
       ▲
       │ async events
┌──────┴──────────────────────────────────────────────┐
│                  Redis Event Bus                    │
│  Workers: sentiments · nightly · reminders ·       │
│           personalization agent · scheduler         │
└──────────────────────────────────────────────────────┘
```

**Key packages:**

| Path | Role |
| --- | --- |
| `app/orchestrator/` | Prompt assembly, persona resolution, LLM dispatch |
| `app/domain/` | Core business logic (conversation, reminders, turns) |
| `app/personality/` | Personality mode definitions and per-user switching |
| `app/workers/` | Background jobs (sentiment, nightly, reminders) |
| `app/features/` | Opt-in feature modules (NSFW prefs, psych tests, Discord, …) |
| `app/rag/` | Semantic memory — embedding, retrieval, vector store |
| `app/interfaces/` | Telegram adapter, admin HTTP server |
| `app/infra/` | DB layer, schema bootstrap, file storage |
| `scripts/` | Bootstrap, migration, import helpers |
| `docs/` | Architecture decisions, runbooks, setup guides |

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.11+ | Tested on 3.11 |
| [Ollama](https://ollama.com) | Local LLM inference; pull a model before starting |
| [Redis](https://redis.io) | Event bus; `redis-server` or Docker |
| Telegram bot token | Create a bot via [@BotFather](https://t.me/BotFather) |

**Minimal Ollama setup:**

```bash
ollama pull llama3          # or any model you prefer
ollama pull nomic-embed-text  # embedding model (required for RAG)
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/wellness-bot.git
cd wellness-bot

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — at minimum set TELEGRAM_BOT_TOKEN, ADMIN_USERNAME, ADMIN_PASSWORD

# 5. Bootstrap the database and data directories
python scripts/bootstrap.py --ensure-dirs --init-db --init-vector

# 6. Start
python -m app.main_modular
```

The bot is now polling Telegram. Open a chat with your bot and send `/start`.

The admin web panel is available at `http://localhost:8000/admin` (port configurable via uvicorn args).

---

## Configuration

All configuration is via environment variables loaded from `.env`. See [`.env.example`](.env.example) for the full reference with descriptions.

**Essential variables:**

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ADMIN_USERNAME` | Your Telegram username — grants admin panel access |
| `ADMIN_PASSWORD` | Admin panel login password |
| `DATA_ROOT` | Directory where all user data and databases are stored |
| `DATABASE_PATH` | Full path to the SQLite database file |
| `CHAT_MODEL` | Ollama model for conversations (e.g. `llama3:latest`) |
| `EMBED_MODEL` | Ollama embedding model (e.g. `nomic-embed-text`) |
| `REDIS_URL` | Redis connection string |

**Feature flags** are controlled via the `APP_FEATURE_FLAGS` JSON variable. Set a key to `false` to disable a feature without removing code.

---

## Personality Modes

Modes are defined in [`app/personality/modes.py`](app/personality/modes.py). Each mode has its own system prompt, temperature, and feature flags (e.g. whether reminders or psych-profiling are active).

Built-in modes: `professional`, `friendly`, `creative`, `therapeutic`, `workfocus`, `roleplay`, `downbad`.

> **Content note:** `downbad` mode enables explicit adult content. It is gated behind a per-user NSFW opt-in and is off by default. Set `"nsfw_preferences": false` in `APP_FEATURE_FLAGS` to disable it entirely.

Users switch modes via in-chat commands; admins can override per-user via the admin panel.

---

## Discord Integration

1. Create an application at [discord.com/developers](https://discord.com/developers/applications)
2. Add `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_PUBLIC_KEY` to `.env`
3. Set `"discord_bot": true` in `APP_FEATURE_FLAGS`
4. See [`docs/DISCORD_BOT_SETUP_GUIDE.md`](docs/DISCORD_BOT_SETUP_GUIDE.md) for slash command registration and invite links

---

## Image Generation (Optional)

Three backends are supported, all optional:

| Backend | Notes |
| --- | --- |
| **FLUX.2 Klein** | Local GPU inference via a sidecar FastAPI server; see `flux2kleinpipeline.md` |
| **EasyDiffusion** | Point `EASY_DIFFUSION_URL` at a running EasyDiffusion instance |
| **Perchance** | Browser-based; requires `playwright install chromium` after pip install |

Uncomment the relevant section in `requirements.txt` for local diffusion support.

---

## Running as a Service (Windows)

See [`docs/windows_services.md`](docs/windows_services.md) for creating Task Scheduler entries to run the main bot and background workers on startup.

## Running as a Service (Linux / systemd)

See the `systemd/` directory for service unit templates.

---

## Development

```bash
# Lint
ruff check app/ tests/

# Type-check
mypy app/

# Run tests
pytest tests/ -v

# Run a single worker manually (useful for debugging)
python -m app.workers.nightly
python -m app.workers.sentiments
```

See [`docs/testing.md`](docs/testing.md) for the full test matrix and acceptance criteria.

---

## Project Layout

```text
wellness-bot/
├── app/
│   ├── config.py             # Pydantic settings (reads .env)
│   ├── main_modular.py       # Entry point
│   ├── orchestrator/         # Prompt building, LLM dispatch
│   ├── domain/               # Business logic
│   ├── personality/          # Mode definitions and manager
│   ├── features/             # Feature modules
│   ├── workers/              # Background jobs
│   ├── rag/                  # Semantic memory / RAG
│   ├── interfaces/           # Telegram + admin HTTP
│   └── infra/                # DB, schema, files
├── docs/                     # Guides, ADRs, runbooks
├── scripts/                  # Bootstrap, migrations
├── tests/                    # pytest suite
├── systemd/                  # Linux service units
├── flux2kleinpipeline.md     # Local FLUX image gen sidecar
├── .env.example              # Configuration reference
└── requirements.txt
```

---

## Security Notes

- **Never commit `.env`** — it contains live credentials. It is gitignored by default.
- The `wellness_data/` directory contains user PII (conversations, profiles). It is gitignored.
- `ENABLE_DANGEROUS_TOOLS`, `ADMIN_DB_EDIT_ENABLED`, `ADMIN_LLM_CONSOLE_ENABLED`, and `ADMIN_OMNI_BROADCAST_ENABLED` default to `false`. Only enable them in controlled environments.
- Admin passwords are bcrypt-hashed on first use. The hash file is in `wellness_data/` and is not committed.
- See [`docs/secrets.md`](docs/secrets.md) for credential rotation procedures.

---

## Contributing

Pull requests welcome. Please run `ruff check` and `mypy` before submitting. For larger changes, open an issue first to discuss the approach.

---

## License

MIT — see `LICENSE` if present, otherwise contact the repository owner.
