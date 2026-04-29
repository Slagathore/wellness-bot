Operational Baseline (To Collect)
=================================

Metrics to Capture
- Message handling latency: p50/p95/p99.
- Reminder delivery: on-time %, duplicates %, average skew.
- Crash-free sessions per day; restart causes.
- Resource: memory (RSS), CPU %, thread/task count over 15–60m soak.
- Log volume: lines/sec by level; top 10 error/warn categories.
- Dependency health: DB/vector/LLM availability and typical latency.

Process
- Run current stack under representative load (scripted chats + reminders) for 15–30m.
- Record metrics and screenshots of dashboards/log samples.
- Note any observed leaks (increasing RSS/threads) or stalls.

Targets (draft)
- Match or beat p95 2.5s, p99 5s; reminder on-time ≥98%; crash-free ≥99.5%.

Status
- Pending capture.
