ADR 0004: Admin Surface Direction
=================================

Context
-------
Admin controls live in tkinter inside `unified_bot.py`, tightly coupled to runtime state and threading. Operators need secure, auditable, remote-friendly access with minimal coupling to runtime threads.

Decision
--------
- Move admin UI to a web surface (FastAPI + templated dashboard or lightweight React front-end) served alongside ops endpoints.
- Keep a minimal desktop launcher for local operators if required, delegating all actions via HTTP/API to the admin service.
- Enforce auth (session or token), RBAC for sensitive actions, and audit logging for all admin mutations.

Consequences
------------
- Decouples operator UX from runtime threads; enables headless/server deployments.
- Introduces HTTP surface attack surface; mitigated via auth/RBAC/audit and TLS.
- tkinter code will be gradually retired; interim period may keep a read-only status panel if needed.

