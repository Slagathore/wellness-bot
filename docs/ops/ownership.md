Ownership & Codeowners Outline
==============================

Proposed Areas
- core/ (config, DI, event bus, telemetry): Platform
- infra/db, infra/vector, infra/llm, infra/files: Platform + Data/ML
- domain/conversation, domain/reminders, domain/personality, domain/moderation, domain/workfocus, domain/outbox: Domain Leads
- interfaces/telegram, interfaces/admin, interfaces/http: Surfaces Team
- workers/ (scheduler jobs, queues): Platform + Domain for respective jobs
- docs/, runbooks, ADRs: Architecture + SRE

Notes
- Formal CODEOWNERS file to be added after directories land and owners confirmed.
- Each package should carry a README with its contracts and contact.
