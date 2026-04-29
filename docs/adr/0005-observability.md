ADR 0005: Observability Standards
=================================

Context
-------
Logs are inconsistent and unstructured; metrics/tracing are partial. Debugging latency, crashes, or missed reminders is hard.

Decision
--------
- Standardize structured logging (JSON) with correlation IDs (per request/session) and redaction of PII/secret-bearing fields.
- Emit metrics for latency, errors, queue depth, retries, memory/CPU, and scheduler job outcomes; expose /metrics for scraping.
- Add tracing spans around DB, vector, LLM, and external I/O; propagate context through event bus and workers.
- Provide diagnostics bundle exporter (recent logs + health snapshot + top counters) for support.

Consequences
------------
- Easier root cause analysis and SLO enforcement.
- Slight overhead; acceptable within latency budgets.
- Requires log processors/collectors in deployment environments; local dev uses console/json logs.

