Migration Checklist (Monolith → Modular Runtime)
================================================

Prep
- Baseline metrics captured (docs/ops/baseline.md) and SLO targets agreed.
- ADRs approved: event bus, DI/lifecycle, worker model, admin surface, observability.
- Feature freeze on non-critical changes.

Step 1: Core Rails
- Configure logging (JSON + correlation IDs) and event bus/lifecycle via `app/core/*`.
- Wire bootstrap entrypoint (`app/runtime/bootstrap.py`) to load config and start bus/scheduler.

Step 2: Infra Ports
- Define DB/vector/LLM/file interfaces under `app/infra/` and provide adapters with retries/timeouts/metrics.
- Add health probes for each dependency.

Step 3: Domain Extraction
- Move reminder/conversation/personality/moderation/workfocus/outbox logic into `app/domain/` services.
- Add unit tests with fakes for infra ports; golden tests for critical flows.
- Ensure onboarding gate routes pre-onboard users via `OnboardingService` before conversation handlers.

Step 4: Surfaces
- Convert Telegram handlers to thin adapters that publish events and render responses.
- Stand up admin web (FastAPI) endpoints for status/actions; deprecate tkinter mutating controls.

Step 5: Scheduling/Workers
- Register reminder/outbox/workfocus jobs in scheduler; ensure idempotency keys and DLQ.
- Add metrics on job duration, failures, misfires; expose on /metrics.

Step 6: Observability & Security
- Structured logs + tracing spans; redaction in place.
- Audit log for admin actions; RBAC on admin endpoints; webhook/signature for external calls.

Step 7: Cutover
- Shadow + dual-write; canary rollout; rollback plan; archive `unified_bot.py` after burn-in.
- Verify Prometheus scrape, tracing/audit enabled, and moderation/safety events surfaced in admin.
- Update CODEOWNERS and runbooks; verify DSAR/export/delete flows.
