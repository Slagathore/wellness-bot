# Secrets and Environment Variables

The application reads configuration via `.env` using `app/config.py`.

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. |
| `REDIS_URL` | Redis connection string (must include DB index). |
| `OLLAMA_HOST` | Base URL where Ollama is running. |
| `EMBED_MODEL` | Ollama embedding model name, defaults to `nomic-embed-text`. |
| `ADMIN_USERNAME` | Telegram username allowed to access admin APIs. |
| `DATA_ROOT` | Filesystem root for user data, backups, transcripts. |
| `DATABASE_PATH` | Absolute path to SQLite database file. |
| `VECTOR_BACKEND` | `sqlite-vec` or `sqlite-vss`. |
| `CTX_TOKEN_BUDGET` | Max tokens for prompt context. |
| `TOP_K_RETRIEVAL` | Number of vector matches to fetch. |

## Rotating Secrets
- Regenerate bot token via @BotFather and update `.env`.
- Rotate Redis password (if using ACL) and update `REDIS_URL`.
- If hosting Ollama behind reverse proxy, store credentials in OS keychain and reference via environment variables.

## Storage Permissions
Ensure the process user (e.g., `telegram-bot`) owns `DATA_ROOT` and has read/write access to:
- user directories under `DATA_ROOT/users/`
- backup directory `DATA_ROOT/backups/`
- SQLite file defined in `DATABASE_PATH`

## Safe Sharing
Never commit `.env` to source control. Use `.env.example` to communicate required keys.
