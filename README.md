# Mira, a self-hosted wellness bot

Mira is an AI wellness companion you run yourself. It talks over Telegram, and optionally Discord, and does its thinking with local LLMs through [Ollama](https://ollama.com). Nothing has to touch a cloud service unless you decide it should.

## What it does

- Holds a real conversation and remembers it. Context and memory persist across sessions, not just within one chat.
- Ships several personality modes (Professional, Friendly, Creative, Therapeutic, Work Focus, Roleplay, and more). Each user picks their own.
- Lets you import or build custom personas, stored in the database.
- Tracks sentiment quietly in the background and builds a psych profile from the conversations over time.
- Sets and delivers its own follow up reminders based on what you talked about.
- Runs a nightly worker that summarizes the day's sentiment trends.
- Uses semantic memory (RAG): older conversations get embedded and pulled back in when they are relevant.
- Can generate images, if you want it, through the standalone DungeonMaster SDXL server (per engine recipes plus LoRA stacking).
- Comes with an admin web panel (FastAPI) for user management, broadcasts, and an LLM console.
- Runs a Discord bot alongside Telegram if you turn it on.
- Includes adaptive in chat psych tests that feed back into the user profile.

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
│   (app/orchestrator/)  -  context builder, prompt    │
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
│           In-Process Event Bus (asyncio)            │
│  Workers: sentiments · nightly · reminders ·       │
│           personalization agent · scheduler         │
└──────────────────────────────────────────────────────┘
```

**Key packages:**

| Path | Role |
| --- | --- |
| `app/orchestrator/` | Prompt assembly, persona resolution, LLM dispatch |
| `app/domain/` | Core business logic (conversation, reminders, turns) |
| `app/personality/` | Personality mode definitions and per user switching |
| `app/workers/` | Background jobs (sentiment, nightly, reminders) |
| `app/features/` | Opt in feature modules (NSFW prefs, psych tests, Discord, and so on) |
| `app/rag/` | Semantic memory: embedding, retrieval, vector store |
| `app/interfaces/` | Telegram adapter, admin HTTP server |
| `app/infra/` | DB layer, schema bootstrap, file storage |
| `scripts/` | Bootstrap, migration, import helpers |
| `docs/` | Architecture decisions, runbooks, setup guides |

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.11+ | Tested on 3.11 |
| [Ollama](https://ollama.com) | Local LLM inference; pull a model before starting |
| Telegram bot token | Create a bot via [@BotFather](https://t.me/BotFather) |

The event bus runs in the same process (`asyncio`, see [`app/core/events.py`](app/core/events.py)), so Redis is not required. `redis` is pinned in `requirements.txt` and `REDIS_URL` exists as a reserved setting, but nothing in `app/` currently connects to Redis.

**Minimal Ollama setup:**

```bash
ollama pull llama3          # or any model you prefer
ollama pull nomic-embed-text  # embedding model (required for RAG)
```

## Quick start

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
# Edit .env, at minimum set TELEGRAM_BOT_TOKEN, ADMIN_USERNAME, ADMIN_PASSWORD

# 5. Bootstrap the database and data directories
python scripts/bootstrap.py --ensure-dirs --init-db --init-vector

# 6. Start
python -m app.main_modular
```

The bot is now polling Telegram. Open a chat with it and send `/start`.

The admin web panel lives at `http://localhost:8110/admin` (port is configurable via uvicorn args).

## Configuration

Everything is configured through environment variables loaded from `.env`. See [`.env.example`](.env.example) for the full reference with descriptions.

**The variables you actually need:**

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ADMIN_USERNAME` | Your Telegram username, grants admin panel access |
| `ADMIN_PASSWORD` | Admin panel login password |
| `DATA_ROOT` | Directory where all user data and databases are stored |
| `DATABASE_PATH` | Full path to the SQLite database file |
| `CHAT_MODEL` | Ollama model for conversations (e.g. `llama3:latest`) |
| `EMBED_MODEL` | Ollama embedding model (e.g. `nomic-embed-text`) |
| `REDIS_URL` | Reserved, not currently used (the event bus runs in process). |

Feature flags live in the `APP_FEATURE_FLAGS` JSON variable. Set a key to `false` to switch a feature off without touching the code.

## Personality modes

Modes are defined in [`app/personality/modes.py`](app/personality/modes.py). Each one carries its own system prompt, temperature, and feature flags (for example whether reminders or psych profiling are on).

Built in modes: `professional`, `friendly`, `creative`, `therapeutic`, `workfocus`, `roleplay`, `downbad`.

Heads up: `downbad` mode enables explicit adult content. It sits behind a per user NSFW opt in and is off by default. Set `"nsfw_preferences": false` in `APP_FEATURE_FLAGS` to kill it entirely.

Users switch modes with in chat commands. Admins can override any user from the admin panel.

## Discord integration

1. Create an application at [discord.com/developers](https://discord.com/developers/applications)
2. Add `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_PUBLIC_KEY` to `.env`
3. Set `"discord_bot": true` in `APP_FEATURE_FLAGS`
4. See [`docs/DISCORD_BOT_SETUP_GUIDE.md`](docs/DISCORD_BOT_SETUP_GUIDE.md) for slash command registration and invite links

## Image generation (optional)

Image generation is handled by the standalone DungeonMaster SDXL server (`python dm_imagegen.py --serve`, default `127.0.0.1:8500`). The bot POSTs a prompt and gets a PNG back. No torch or diffusers run in this process, so startup stays light.

- Point the bot at it with `DM_IMAGE_URL` (default `http://127.0.0.1:8500`) and toggle it with `DM_IMAGE_ENABLED`.
- Recipes route by style and rating (realism goes to jugg/lustify, anime to wai, anthro to pony) with matching LoRA stacking.
- If the server is not running, image buttons hide and `/imagine` says so. Nothing else breaks.

## Running as a service (Windows)

See [`docs/windows_services.md`](docs/windows_services.md) for Task Scheduler entries that start the main bot and background workers on boot.

## Running as a service (Linux / systemd)

See the `systemd/` directory for service unit templates.

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

## Project layout

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
├── .env.example              # Configuration reference
└── requirements.txt
```

## Security notes

- Never commit `.env`. It holds live credentials and is gitignored by default.
- The `wellness_data/` directory holds user PII (conversations, profiles). It is gitignored.
- `ENABLE_DANGEROUS_TOOLS`, `ADMIN_DB_EDIT_ENABLED`, `ADMIN_LLM_CONSOLE_ENABLED`, and `ADMIN_OMNI_BROADCAST_ENABLED` all default to `false`. Only turn them on in environments you control.
- Admin web panel auth: the FastAPI admin server (`app/interfaces/admin/server.py`) uses HTTP Basic against `ADMIN_USERNAME` and `ADMIN_PASSWORD`, compared in plaintext. Keep `.env` secret, and run the panel on loopback (the default is now `127.0.0.1`) behind a TLS reverse proxy if it has to be reachable remotely. The salted hash `AdminAuth` in `app/utils/security.py` (SHA-256, not bcrypt) currently guards only the desktop Tk control panel, not the web panel.
- See [`docs/secrets.md`](docs/secrets.md) for credential rotation.

## Contributing

If you want to send a patch, run `ruff check` and `mypy` first. For anything bigger than a small fix, open an issue so we can talk through the approach before you write it.

## License

MIT. See `LICENSE` if present, otherwise contact the repository owner.
