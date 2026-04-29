# Wellness Bot Setup Guide

## Prerequisites
- Python 3.11+
- Redis server
- Ollama with required models (`llama3.1:8b`, `nomic-embed-text`)
- nginx with SSL certificates (Let''s Encrypt recommended)

## Bootstrap Steps
1. Copy `.env.example` to `.env` and fill in all required values (see `docs/secrets.md`).
2. Run `python scripts/bootstrap.py --ensure-dirs --init-db --init-vector`.
3. (Optional) Seed an admin user:
   ```bash
   python scripts/bootstrap.py --create-admin <TELEGRAM_USER_ID> --username your_username
   ```
4. Copy `systemd/*.service` into `/etc/systemd/system/` and enable the runtime services:
   ```bash
   sudo systemctl enable --now telegram-runtime telegram-admin telegram-outbox telegram-embeddings telegram-sentiments telegram-nightly
   ```
5. Apply the nginx template if you are hosting admin endpoints behind a reverse proxy.
6. No webhook registration is required for polling mode; the modular runtime uses long polling.

## Testing
See `docs/testing.md` for full pytest commands and manual QA checklist.

## Windows Services
See `docs/windows_services.md` for Task Scheduler setup.


## Monitoring & Maintenance
See `docs/monitoring.md` for journald tails, Redis inspection, nightly output locations, and log rotation tips.
