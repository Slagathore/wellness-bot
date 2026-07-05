# Mira Wellness Bot — Audit & Remediation Log

**Started:** 2026-07-05
**Branch:** `fix/crisis-safety-and-cleanup`
**Scope of audit:** full `app/` tree (~58k LOC), tests, CI, tooling, admin UI (ran live + screenshotted), git history and repo hygiene. Excludes `.venv`, caches, `.git`.

This document is the running record of what the audit found and what is being changed. It is edited continuously as work lands. Status legend: ☐ todo · ◐ in progress · ☑ done.

---

## TL;DR verdict

Ambitious, genuinely featureful product (~58k LOC) that is **mid-rewrite and never finished the migration** — legacy subsystems were left in place alongside their replacements, so the repo ships three parallel config/DB/RAG stacks, three god-files (~32% of `app/`), and a **broken crisis-handling path in a mental-health bot**. The modern parts (event bus, DI container, shadow-mode planner rollout, LLM param precedence) are well-designed. Git history is clean — no secret was ever committed.

---

## Clean bill of health (verified)

- `.env` was **never** committed; no real secret exists in any of the 5 pushed commits (all matches are placeholders).
- `wellness.db`, `wellness_data/`, the 17 MB `perchance-*.json`, `.snapshots/`, `.kilo/`, `nul` — all correctly untracked and covered by `.gitignore`.

---

## Findings by severity

### P0 — Crisis handling (safety-critical for a wellness bot)

1. **A user expressing suicidal ideation gets "Please slow down; I'm processing your recent messages."**
   [app/domain/safety/filter.py](app/domain/safety/filter.py) `allow()` returns a single bool for two unrelated conditions — rate-limiting *and* crisis-keyword match — discarding which fired. The Telegram fast path ([app/interfaces/telegram/adapter.py:6522-6572](app/interfaces/telegram/adapter.py#L6522-L6572)) treats any `False` as a throttle: it sends the throttle text and `return`s **before** `safety_service.inspect_message()` at line 6570 can run. Because `inspect_message` uses the identical keyword list, it only ever sees text the gate already proved is *not* crisis — so real-time keyword crises are never logged to the admin "Crisis Alerts" dashboard. Same bug duplicated in the event-bus path [app/domain/conversation/handler.py:56-93](app/domain/conversation/handler.py#L56-L93).
2. **Crisis detection is disabled entirely in `roleplay` and `downbad` modes** — the exact modes where a vulnerable user is most exposed. [app/history_scope.py:122-125](app/history_scope.py#L122-L125) `automated_moderation_allowed_for_scope()` returns `True` only for `standard`; both the real-time filter and the batch worker skip crisis detection otherwise.
3. **`EVENT_CRISIS_DETECTED` has zero subscribers** — even when it fires, the only effect is one DB row awaiting manual review. No real-time escalation.
4. **Crisis resources (988, Crisis Text Line, Trans Lifeline) exist only as a RAG document** — delivery depends on vector similarity surfacing that doc, not a guaranteed path.
5. `tests/test_e2e_crisis.py` hand-inserts DB rows instead of exercising the filter/handler, so none of the above was ever caught.
5b. **(Found while fixing)** Even when `inspect_message` *did* run, it inserted the crisis event with `severity = 7`, but `moderation_events` has `CHECK (severity BETWEEN 1 AND 5)` — so the insert silently failed (swallowed by the repo) and the crisis was **never persisted**, independent of the short-circuit. The batch workers correctly use `5`. Fixed to `5`.

### P1 — Security (admin panel)

6. **LLM Console ≈ RCE + total data loss behind one plaintext password.** `query_database`/`update_database` let the model run arbitrary SQL (`DELETE FROM users` passes the guard); `edit_file` writes any file under the project root. [app/interfaces/admin/llm_console_tools.py:330-420](app/interfaces/admin/llm_console_tools.py#L330-L420)
7. **`/highrisk/db_edit` `where` clause is string-interpolated SQL** — only defense is rejecting `;`, which classic injection doesn't need. [app/interfaces/admin/server.py:2102](app/interfaces/admin/server.py#L2102)
8. **Admin server binds `0.0.0.0` by default**, plain HTTP, Basic auth against the **plaintext** `.env` password (README's "bcrypt-hashed" claim is false for the web GUI). [app/interfaces/admin/server.py](app/interfaces/admin/server.py) + `systemd/telegram-admin.service`
9. **No CSRF protection**; `/models/pull/stream` is a state-changing GET that spawns a subprocess.
10. **[app/admin/api.py](app/admin/api.py) never checks the password** — any password authenticates. Dead/orphaned code, but a live landmine in a public repo.
11. **`.dockerignore` does not exclude `.env`** — `COPY . .` in the Dockerfile bakes real tokens into image layers.
12. **`.claude/settings.local.json` is tracked and pushed** to the public repo (leaks local absolute paths + tooling, not credentials).
13. A "Legacy" admin button sends the literal string `(admin) stub broadcast` to all real Telegram + Discord users if clicked through its confirms. [app/interfaces/admin/server.py:5199-5204](app/interfaces/admin/server.py#L5199-L5204)

### P1 — Dead / stubbed / misleading

14. **Docs lie about architecture:** README advertises a "Redis Event Bus" as a first-class component and hard prerequisite; Redis is never imported anywhere. Real bus is an in-process `asyncio.Queue`.
15. **~4,500+ lines of deletable dead weight:** legacy inline-HTML admin UI [server.py:3582-5708](app/interfaces/admin/server.py#L3582-L5708) (~2,100 lines, only renders if `admin.html` is deleted); `app/admin/api.py`; `NullConversationRepository` (zero references); unreachable `/character` block referencing undefined `characters` (all 3 ruff F821s) [adapter.py:2311-2344](app/interfaces/telegram/adapter.py#L2311-L2344); empty `optimize_shards()` the nightly job calls forever [nightly.py:150-152](app/workers/nightly.py#L150-L152).
16. **Venv doesn't match `requirements.txt`:** `ollama`/`numpy` pinned but not installed → 9 test modules fail collection and the live RAG backend errors ("numpy is required for NumpyVectorBackend").
17. **Tests: 812 pass, 18 fail, 9 collection errors.** `test_onboarding_flow_progression` asserts copy the flow no longer produces — an environment-independent failure, so **CI is likely red**. CI runs no lint/mypy. Pre-commit config references directories (`unified_bot/`, `handlers/`) and a file that no longer exist. ~50 deps all `>=`, no lockfile.

### P2 — Architecture (maintainability tax, not live defects)

18. Sync/async twin pipelines ~90% identical (`generate_response_async` vs `_generate_response_sync`, ~400 lines each).
19. Three DB layers incl. raw unpooled `sqlite3.connect()` on **every chat turn** in [app/personality/manager.py](app/personality/manager.py) — a real `SQLITE_BUSY` risk.
20. Two non-interoperating RAG stacks (real `sqlite-vec` vs. a hand-rolled pure-Python cosine loop against a second hardcoded-path DB).
21. **Discord runs the legacy 165-line pipeline** — silently lacks turn planning, continuation, live search.
22. Three god-files: `adapter.py` (6,749), `control_panel.py` (6,124), `server.py` (5,737).
23. 343 broad `except Exception`, ~30 swallow with `pass` (incl. persona resolution → silent degrade to "friendly").

### UI (ran live, screenshotted all tabs)

Solid, coherent ops panel (B/B+): consistent dark design system, strong information architecture (11 tabs), standout Media tab. Weaknesses: poor vertical-space discipline (cards stack full-width leaving dead space), weak empty/zero states, danger-red diluted by using it for primary CTAs too, live-feed SSE disconnects immediately (`ERR_INCOMPLETE_CHUNKED_ENCODING`), Chart.js loads from a CDN (breaks the "no cloud deps" pitch + offline), 5,111-line single `admin.js`, and **three UIs total** (this one + the dead inline fallback + the Tkinter panel).

---

## Remediation plan (this branch)

### Immediate — in progress

- ☑ **Crisis path fix** (commit `dae…`)
  - ☑ Split `SafetyFilter` into a rate-limit-only gate returning a structured `SafetyDecision`; a crisis never blocks the message.
  - ☑ Made `SafetyService.inspect_message` run in **all** scopes and return whether a crisis was flagged; fixed the `severity 7 → 5` CHECK-constraint bug.
  - ☑ Send the real crisis-resource message (988 / 741741 / Trans Lifeline / findahelpline / 911) on detection, then continue to a normal empathetic reply — in both the fast path and the event-bus `SafetyEventHandler`.
  - ☑ Added `CrisisAlertHandler` subscribed to `EVENT_CRISIS_DETECTED` (structured real-time WARNING escalation hook), registered in wiring.
  - ☑ Added `tests/test_safety_crisis_path.py` — drives the filter + service incl. `downbad` scope; 6 tests pass.
- ☑ **Deletion sweep** (commit `pending`): removed the ~2,125-line legacy inline-HTML fallback in `server.py` (now serves `admin.html` unconditionally, 500s if missing) — this also removed the `(admin) stub broadcast`/`stub prompt` buttons and the "old stub tools" panel, which lived inside it; deleted `app/admin/` (dead `api.py` with the password-bypass bug + empty `__init__`); removed the unreachable `/character` pagination block (all 3 ruff F821s); deleted `NullConversationRepository`; removed the empty `optimize_shards()` no-op and its nightly call; deleted the stray `nul` file. `server.py`: 5,737 → 3,612 lines.
- ☑ **Security batch** (commit `pending`): admin server now defaults to `127.0.0.1` (`run()`, CLI `--host`, and `systemd/telegram-admin.service`), with a WARNING when bound non-loopback; `/highrisk/db_edit` WHERE is now parsed to a strict parameterized `<pk> = <id>` / `<pk> IN (...)` form (`_parse_pk_where`, + tests) instead of raw string interpolation; `.dockerignore` rewritten to exclude `.env`/`.env.*`/`*.hash`/`*.key`/`.snapshots`/`.kilo`/`.claude`/`perchance*.json`/`nul`; `.claude/settings.local.json` untracked (`git rm --cached`) and added to `.gitignore`.
  - Not done here (larger, deferred): hashing the admin web password, adding CSRF tokens, converting `/models/pull/stream` to POST, and restricting the LLM-console SQL/file tools — tracked in the deferred list below.
- ☑ **CI/docs repair** (commit `pending`):
  - Fixed `test_onboarding_flow_progression` — the flow gained a `sleep_schedule` step between `timezone` and `support_preferences`; the test now exercises it. Passes.
  - README: replaced the fictional "Redis Event Bus" (diagram + prerequisite + `REDIS_URL` note) with the real in-process `asyncio` bus, and corrected the false "bcrypt-hashed" admin-password claim (web panel uses plaintext HTTP Basic; the SHA-256 `AdminAuth` guards only the desktop panel).
  - `.pre-commit-config.yaml`: dropped the nonexistent `unified_bot`/`handlers` path globs and the broken `python test_feature_flags.py` local hook (file doesn't exist); bumped ruff to v0.6.9.
  - Added a `ruff.toml` and made `app/` + `tests/` ruff-clean (0 errors, down from 22 — auto-fixed 11 unused-import/empty-f-string, hand-fixed the ambiguous name + 3 unused vars, ignored the intentional E402 in the Discord bootstrap).
  - CI (`ci.yml`): added a `lint` job running ruff (blocking) and mypy (informational).
- ◐ **Verify:** targeted suites pass (safety, db_edit, onboarding, reminders, cloud-drain = 44 + 6 + 11); full-suite run pending in the verify step.

### Deferred (larger, tracked but not in this pass)

- Unify sync/async pipelines into one async impl + sync wrapper.
- Point Discord at the modern pipeline.
- Collapse to one DB layer and one RAG stack.
- UI polish: card grids, empty states, destructive-vs-primary styling, vendor Chart.js, fix SSE reconnect.
- Decompose `adapter.py` and decide the Tkinter panel's fate.

---

## Change log

- **CI/docs repair** — fixed the stale onboarding test (new `sleep_schedule` step); corrected README's Redis-bus and bcrypt claims; fixed stale/broken pre-commit config; made `app/`+`tests/` ruff-clean (22 → 0) with a new `ruff.toml`; added a CI `lint` job (ruff blocking, mypy informational).
- **Security batch** — admin bind defaults to loopback (`run()`/CLI/systemd); `db_edit` WHERE parameterized to a strict PK form (`_parse_pk_where` + tests); `.dockerignore` now excludes `.env`/secrets/PII/bulky exports; `.claude/settings.local.json` untracked + ignored.
- **Deletion sweep** — removed ~2,300 lines of dead code: legacy inline-HTML admin fallback (+ its stub broadcast/console buttons), `app/admin/api.py` (+ package), unreachable `/character` block (fixes 3 F821s), `NullConversationRepository`, empty `optimize_shards()`, stray `nul`. `server.py` 5,737 → 3,612.
- **Crisis path fix** — `SafetyFilter` split into a rate-limit-only gate + `SafetyDecision`; `SafetyService.inspect_message` now runs in every scope, returns a flag, and uses severity 5 (was 7, silently rejected by the CHECK constraint); crisis-resource message sent on the fast path and via `SafetyEventHandler`; `CrisisAlertHandler` subscribes to the previously-dead `EVENT_CRISIS_DETECTED`; new `tests/test_safety_crisis_path.py` (6 passing). Files: `app/domain/safety/{filter,service,handler,resources}.py`, `app/interfaces/telegram/adapter.py`, `app/runtime/wiring.py`.
