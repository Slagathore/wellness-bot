Data & Secrets Inventory (Draft)
================================

PII / User Data
- User profiles/personality data: DB tables (`profiles`, similar); vector embeddings; retention policy needed.
- Conversation history: DB + vector embeddings; redaction required for logs; deletion/export flow for DSAR.
- Reminders: DB schedule entries; timezones; ensure correct retention/deletion on account removal.
- Onboarding responses and safety preferences: DB; must be covered by consent tracking.

Secrets / Keys
- Telegram bot token, Discord token (if any), LLM API keys, vector backend credentials.
- DB connection strings/paths; admin auth secrets; webhook signing keys.

Flows & Storage
- Primary DB: `wellness_data/wellness.db` (SQLite WAL). Backups in `wellness_data/backups/`.
- Vector store: via `app.vector_backends`; confirm backend (local vs remote).
- Logs: `wellness_data/bot.log` and exports; must redact PII/secrets.

Controls (to implement/enforce)
- Centralized secret loader (no scattered `os.getenv`); fail fast on missing/invalid.
- Encryption at rest for backups or store in protected path; secure deletion for exports.
- Redaction middleware for logs and traces; avoid persisting full prompts with PII.
- RBAC + audit logging for admin actions; signed webhooks; TLS for HTTP surfaces.
- Retention/deletion: configurable TTL for messages/profiles; DSAR export pipeline.
