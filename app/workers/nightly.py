"""Mission Statement:
Execute the wellness platform's nightly maintenance loop so user care stays
consistent, recoverable, and insight-rich. This worker backs up data, rebuilds
analytics, refreshes resources, and regenerates psychological profiles using
our centralized personality tooling. By orchestrating these hygiene tasks in a
single pass we preserve conversational context, surface new recommendations,
and keep crisis monitoring signals current for the next day."""

from __future__ import annotations

import json
import math
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from app.analytics.emotional_analyzer import EmotionalAnalyzer
from app.config import settings
from app.db import db_ro, db_rw
from app.feature_flags import enabled
from app.history_scope import (HISTORY_SCOPE_STANDARD,
                               automated_moderation_allowed_for_scope,
                               inferred_history_scope_for_message,
                               table_has_column)
from app.memory import ConversationMemoryIndexer
from app.orchestrator.context_builder import invalidate_profile_context_cache
from app.personality.profile_generation import (ProfileGenerationError,
                                                generate_comprehensive_profile)
from app.domain.turns.audit import append_turn_route
from app.rag.ingestion import ResourceIngester
from app.rag.service import get_vector_store
from app.utils.ollama import generate
from app.utils.llm_json import parse_llm_json
from app.utils.text import embed_text
from app.utils.time_utils import operator_now
from app.vector_backends import get_backend


def _nightly_model() -> str | None:
    cfg = settings()
    return (
        getattr(cfg, "nightly_model", None)
        or getattr(cfg, "planner_model", None)
        or getattr(cfg, "turn_planner_model", None)
        or cfg.worker_model
    )


def _standard_message_scope_sql(message_alias: str = "m") -> tuple[str, str]:
    """Return compatibility SQL for filtering to standard-scope messages."""

    if table_has_column("messages", "scope"):
        return ("", f"COALESCE({message_alias}.scope, 'standard') = 'standard'")
    if table_has_column("sessions", "scope"):
        return (
            f"LEFT JOIN sessions AS sess ON sess.id = {message_alias}.session_id",
            "COALESCE(sess.scope, 'standard') = 'standard'",
        )
    if table_has_column("users", "personality"):
        return (
            f"LEFT JOIN users AS u_scope ON u_scope.id = {message_alias}.user_id",
            "LOWER(COALESCE(u_scope.personality, 'standard')) NOT IN ('downbad', 'roleplay')",
        )
    return ("", "1 = 1")


def _message_scope_select_sql(message_alias: str = "m") -> tuple[str, str]:
    """Return compatibility SQL for selecting a message scope-like value."""

    if table_has_column("messages", "scope"):
        return ("", f"COALESCE({message_alias}.scope, 'standard') AS scope")
    if table_has_column("sessions", "scope"):
        return (
            f"LEFT JOIN sessions AS sess ON sess.id = {message_alias}.session_id",
            "COALESCE(sess.scope, 'standard') AS scope",
        )
    if table_has_column("users", "personality"):
        return (
            f"LEFT JOIN users AS u_scope ON u_scope.id = {message_alias}.user_id",
            (
                "CASE "
                "WHEN LOWER(COALESCE(u_scope.personality, 'standard')) IN ('downbad', 'roleplay') "
                "THEN LOWER(u_scope.personality) "
                "ELSE 'standard' END AS scope"
            ),
        )
    return ("", f"'{HISTORY_SCOPE_STANDARD}' AS scope")


def nightly_pipeline() -> None:
    """Run the complete nightly job sequence once."""

    print(f"[nightly] Pipeline started at {operator_now().isoformat()}")
    try:
        backup_database()
        backup_user_filesystems()
        optimize_shards()
        reprocess_sentiments()
        generate_missing_embeddings()
        refresh_wellness_resources()
        generate_user_metrics()
        generate_emotional_summaries()
        update_psychological_profiles()  # Analyze user profiles based on new messages
        merge_profile_fact_candidates()
        promote_durable_memories()
        rescore_referenced_memories()
        repair_turn_audits_from_contradictions()
        scan_for_crisis_indicators()
        cleanup_old_data()
        print("[nightly] Pipeline completed successfully")
    except Exception as exc:  # noqa: BLE001
        print(f"[nightly] Pipeline error: {exc}")


def backup_database() -> None:
    cfg = settings()
    timestamp = operator_now().strftime("%Y-%m-%d_%H%M%S")
    backup_dir = Path(cfg.data_root) / "backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(cfg.database_path)
    if db_path.exists():
        shutil.copy2(db_path, backup_dir / db_path.name)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, backup_dir / sidecar.name)

    print(f"[nightly] Database backup stored at {backup_dir}")


def backup_user_filesystems() -> None:
    cfg = settings()
    users_dir = Path(cfg.data_root) / "users"
    if not users_dir.exists():
        return

    timestamp = operator_now().strftime("%Y-%m-%d_%H%M%S")
    backup_dir = Path(cfg.data_root) / "backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive_base = backup_dir / "users_snapshot"
    shutil.make_archive(str(archive_base), "gztar", users_dir)
    print(f"[nightly] User filesystem snapshot saved to {archive_base}.tar.gz")


def optimize_shards() -> None:
    # Placeholder for shard maintenance; currently append-only.
    pass


def reprocess_sentiments(limit: int = 100) -> None:
    _worker_model = _nightly_model()
    scope_join, scope_predicate = _standard_message_scope_sql("m")
    with db_ro() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.message_id, m.content
            FROM sentiments AS s
            JOIN messages AS m ON m.id = s.message_id
            {scope_join}
            WHERE s.confidence < 0.6
              AND {scope_predicate}
              AND s.processed_at >= datetime('now', '-7 days')
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    for row in rows:
        prompt = (
            "Analyze the emotional content of this message. Respond ONLY with valid JSON.\n\n"
            f"Message: \"{row['content']}\"\n\n"
            "Output format:\n"
            "{\n"
            '    "valence": <float between -1.0 and 1.0>,\n'
            '    "arousal": <float between 0.0 and 1.0>,\n'
            '    "dominance": <float between 0.0 and 1.0>,\n'
            '    "emotion_label": "<joy|sadness|anger|fear|disgust|surprise|neutral>",\n'
            '    "confidence": <float between 0.0 and 1.0>,\n'
            '    "crisis_risk": <boolean>\n'
            "}"
        )
        try:
            response = generate(
                prompt=prompt, model=_worker_model, format="json", options={"temperature": 0.3}
            )
            data = parse_llm_json(response["text"])
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Sentiment reprocess failed for {row['id']}: {exc}")
            continue

        with db_rw() as conn:
            conn.execute(
                """
                UPDATE sentiments
                SET valence = ?,
                    arousal = ?,
                    dominance = ?,
                    emotion_label = ?,
                    confidence = ?,
                    processed_at = datetime('now')
                WHERE id = ?
                """,
                (
                    data.get("valence"),
                    data.get("arousal"),
                    data.get("dominance"),
                    data.get("emotion_label"),
                    data.get("confidence"),
                    row["id"],
                ),
            )
            if data.get("crisis_risk"):
                user_scope = (
                    inferred_history_scope_for_message(message_id=int(row["message_id"]))
                    or HISTORY_SCOPE_STANDARD
                )
                if not automated_moderation_allowed_for_scope(user_scope):
                    continue
                conn.execute(
                    """
                    INSERT INTO moderation_events(user_id, event_type, severity, details)
                    SELECT m.user_id, 'crisis_detected', 5, ?
                    FROM messages AS m
                    WHERE m.id = ?
                    """,
                    (
                        json.dumps(
                            {
                                "message_id": row["message_id"],
                                "source": "nightly_sentiment_reprocess",
                                "scope": user_scope,
                            }
                        ),
                        row["message_id"],
                    ),
                )


def generate_missing_embeddings(limit: int = 100) -> None:
    use_memory_v2 = enabled("conversation_memory_v2")
    backend = get_backend()
    memory_indexer = ConversationMemoryIndexer() if use_memory_v2 else None
    scope_join, scope_select = _message_scope_select_sql("m")
    with db_ro() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.user_id, m.content, m.role, {scope_select}
            FROM messages AS m
            LEFT JOIN embedding_links AS el ON el.message_id = m.id
            {scope_join}
            WHERE el.id IS NULL
              AND m.role IN ('user', 'assistant')
              AND m.content <> ''
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    for row in rows:
        try:
            if memory_indexer is not None:
                memory_indexer.index_message(
                    message_id=row["id"],
                    user_id=row["user_id"],
                    scope=row["scope"],
                    role=row["role"],
                    content=row["content"],
                )
            else:
                vector = embed_text(row["content"])
                backend.upsert(row["id"], vector, {"user_id": row["user_id"]})
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Embedding generation failed for {row['id']}: {exc}")


def refresh_wellness_resources() -> None:
    manifest_path = Path(settings().data_root) / "resources" / "manifest.json"
    try:
        store = get_vector_store()
        ingester = ResourceIngester(store)
        added = ingester.refresh_from_manifest(manifest_path)
        if added:
            print(f"[nightly] Refreshed wellness resources (+{added} docs)")
    except Exception as exc:  # noqa: BLE001
        print(f"[nightly] Resource refresh failed: {exc}")


def generate_user_metrics() -> None:
    cfg = settings()
    db_path = cfg.database_path
    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL sqlite_scanner; LOAD sqlite_scanner;")
    con.execute(f"ATTACH '{db_path}' AS sqlite_db (TYPE SQLITE);")

    with db_ro() as conn:
        users = conn.execute(
            """
            SELECT id, telegram_user_id
            FROM users
            WHERE last_active_at >= datetime('now', '-30 days')
            """
        ).fetchall()

    for user in users:
        user_id = user["id"]
        telegram_user_id = user["telegram_user_id"]
        analytics_dir = (
            Path(cfg.data_root)
            / "users"
            / str(telegram_user_id)
            / "derived"
            / "analytics"
        )
        analytics_dir.mkdir(parents=True, exist_ok=True)

        metrics = _compute_user_metrics_duckdb(con, user_id, telegram_user_id)
        output_path = (
            analytics_dir / f"daily_metrics_{operator_now().strftime('%Y-%m-%d')}.json"
        )
        output_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    con.close()


def generate_emotional_summaries() -> None:
    """Create emotional summary files for each active user."""

    analyzer = EmotionalAnalyzer()
    with db_ro() as conn:
        users = conn.execute(
            """
            SELECT id, telegram_user_id
            FROM users
            WHERE last_active_at >= datetime('now', '-30 days')
            """
        ).fetchall()
    for user in users:
        try:
            analyzer.analyze_user(user["id"], user["telegram_user_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Emotional summary failed for user {user['id']}: {exc}")


def update_psychological_profiles() -> None:
    """Analyze psychological profiles for users with new messages since last analysis."""

    print("[nightly] Starting psychological profile updates...")

    # Create psychological_profiles table if it doesn't exist
    with db_rw() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS psychological_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                profile_data TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now')),
                messages_analyzed INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """
        )

    # Find users who need profile updates
    scope_join, scope_predicate = _standard_message_scope_sql("m")
    with db_ro() as conn:
        users = conn.execute(
            f"""
            SELECT
                u.id,
                u.telegram_user_id,
                COUNT(m.id) as new_messages,
                COALESCE(pp.created_at, '1970-01-01') as last_profile_update
            FROM users u
            LEFT JOIN psychological_profiles pp ON pp.user_id = u.id
                AND pp.id = (
                    SELECT MAX(id) FROM psychological_profiles WHERE user_id = u.id
                )
            LEFT JOIN messages m ON m.user_id = u.id
                AND m.role = 'user'
                AND m.timestamp > COALESCE(pp.created_at, '1970-01-01')
            {scope_join}
            WHERE u.last_active_at >= datetime('now', '-60 days')
              AND {scope_predicate}
            GROUP BY u.id
            HAVING new_messages >= 20
            ORDER BY new_messages DESC
        """
        ).fetchall()

    if not users:
        print("[nightly] No users need profile updates")
        return

    print(f"[nightly] Found {len(users)} users with new messages to analyze")

    for user in users:
        user_id = user["id"]
        telegram_user_id = user["telegram_user_id"]
        new_msg_count = user["new_messages"]

        try:
            updated = _analyze_user_psychological_profile(
                user_id, telegram_user_id, new_msg_count
            )
            if updated:
                print(
                    f"[nightly] Updated profile for user {telegram_user_id} ({new_msg_count} new messages)"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Profile analysis failed for user {user_id}: {exc}")


def _analyze_user_psychological_profile(
    user_id: int, telegram_user_id: int, new_msg_count: int
) -> bool:
    """Deep psychological analysis of user's conversation patterns.

    Returns:
        bool: True if a profile was generated and stored successfully.
    """

    # Get recent messages for analysis
    scope_join, scope_predicate = _standard_message_scope_sql("messages")
    with db_ro() as conn:
        # Total user message count (for confidence calculation)
        total_row = conn.execute(
            (
                f"SELECT COUNT(*) AS cnt FROM messages {scope_join} "
                f"WHERE user_id = ? AND role = 'user' AND {scope_predicate}"
            ),
            (user_id,),
        ).fetchone()
        total_message_count = total_row["cnt"] if total_row else 0

        messages = conn.execute(
            f"""
            SELECT content FROM messages
            {scope_join}
            WHERE user_id = ? AND role = 'user' AND {scope_predicate}
            ORDER BY timestamp DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()

    if total_message_count < 20:
        print(
            f"[nightly] Skipping user {user_id} - only {total_message_count} messages available"
        )
        return False

    # Concatenate recent messages
    conversation_sample = "\n".join(
        msg["content"] for msg in messages[:50] if (msg and msg["content"])
    )
    message_count = total_message_count
    if not conversation_sample:
        print(f"[nightly] Skipping user {user_id} - conversation sample empty")
        return False

    analysis_model = (
        getattr(settings(), "nightly_model", None)
        or getattr(settings(), "psych_model", None)
        or settings().worker_model
    )
    try:
        generation = generate_comprehensive_profile(
            conversation_sample,
            message_count,
            model=analysis_model,
        )
        profile = generation.profile
    except ProfileGenerationError as exc:
        print(f"[nightly] Profile analysis failed for user {user_id}: {exc}")
        return False
    # TODO: Persist summary deltas so we can track how each nightly run changes the profile.

    # Compute confidence from message count and extract structured sub-sections
    confidence_score = min(1.0, generation.message_count / 200.0)
    big_five_json = json.dumps(profile.get("big_five", {}))
    mh_json = json.dumps(profile.get("mental_health_indicators", {}))
    cog_json = json.dumps(profile.get("cognitive_metrics", {}))

    # Store in database
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO psychological_profiles
                (user_id, profile_data, messages_analyzed, confidence_score,
                 big_five, mental_health_indicators, cognitive_metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                json.dumps(profile),
                generation.message_count,
                confidence_score,
                big_five_json,
                mh_json,
                cog_json,
            ),
        )

    # Log key findings
    mental_health = profile.get("mental_health_indicators", {})
    cognitive = profile.get("cognitive_metrics", {})

    depression = _safe_metric_value(mental_health, "depression_likelihood", 0.0)
    anxiety = _safe_metric_value(mental_health, "anxiety_likelihood", 0.0)
    est_iq = _safe_metric_value(cognitive, "estimated_iq", 100)

    print(
        f"[nightly] [PSYCH] User {telegram_user_id} Profile: "
        f"Depression:{depression:.2f} Anxiety:{anxiety:.2f} IQ:~{est_iq:.0f}"
    )
    return True


def _safe_metric_value(section: object | None, key: str, default: float) -> float:
    """Read nested metric values while tolerating legacy formats."""

    if not isinstance(section, dict):
        return float(default)
    value = section.get(key, default)
    if isinstance(value, dict):
        return float(value.get("value", default))
    if isinstance(value, (int, float)):
        return float(value)
    return float(default)


def _safe_json_object(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return dict(parsed)
    return {}


def _safe_json_array(raw: object) -> list[object]:
    if not raw:
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return []
    if isinstance(parsed, list):
        return list(parsed)
    return []


def _normalize_text_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def merge_profile_fact_candidates() -> None:
    """Deduplicate profile facts, promote repeated/high-confidence facts, and mark contradictions."""

    promoted_users: set[int] = set()
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                user_id,
                key,
                value,
                confidence,
                contradiction,
                existing_value,
                status,
                created_at
            FROM profile_fact_candidates
            WHERE status IN ('pending', 'merged', 'review_needed', 'promoted')
            ORDER BY user_id, key, created_at DESC, id DESC
            """
        ).fetchall()

    grouped: dict[tuple[int, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (_coerce_int(row["user_id"]), str(row["key"] or "").strip())
        grouped.setdefault(key, []).append(dict(row))

    with db_rw() as conn:
        for (user_id, key), candidates in grouped.items():
            contradictory_ids: list[int] = []
            value_groups: dict[str, list[dict[str, object]]] = {}
            latest_value_for_group: dict[str, str] = {}
            for row in candidates:
                row_id = _coerce_int(row["id"])
                if bool(row["contradiction"]):
                    contradictory_ids.append(row_id)
                    continue
                normalized_value = _normalize_text_key(row["value"])
                if not normalized_value:
                    continue
                value_groups.setdefault(normalized_value, []).append(row)
                latest_value_for_group[normalized_value] = str(row["value"] or "").strip()

            if contradictory_ids:
                placeholders = ",".join("?" for _ in contradictory_ids)
                conn.execute(
                    f"""
                    UPDATE profile_fact_candidates
                    SET status = 'review_needed', updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    contradictory_ids,
                )

            best_group_key: str | None = None
            best_rank: tuple[float, float, int] | None = None
            for normalized_value, group_rows in value_groups.items():
                confidences = [_coerce_float(item.get("confidence")) for item in group_rows]
                avg_confidence = sum(confidences) / max(1, len(confidences))
                count = len(group_rows)
                newest_id = max(_coerce_int(item.get("id")) for item in group_rows)
                rank = (float(count), avg_confidence, newest_id)
                if best_rank is None or rank > best_rank:
                    best_group_key = normalized_value
                    best_rank = rank

            if best_group_key is None or best_rank is None:
                continue

            winning_rows = value_groups.get(best_group_key, [])
            winning_ids = [_coerce_int(item.get("id")) for item in winning_rows]
            winning_value = latest_value_for_group.get(best_group_key, "")
            winner_count = _coerce_int(best_rank[0])
            winner_avg_confidence = _coerce_float(best_rank[1])

            should_promote = winner_count >= 2 or winner_avg_confidence >= 0.78
            losing_ids = [
                _coerce_int(item.get("id"))
                for group_key, group_rows in value_groups.items()
                if group_key != best_group_key
                for item in group_rows
            ]

            if should_promote and winning_value:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, key) DO UPDATE
                    SET value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, key, winning_value),
                )
                promoted_users.add(user_id)
                placeholders = ",".join("?" for _ in winning_ids)
                conn.execute(
                    f"""
                    UPDATE profile_fact_candidates
                    SET status = 'promoted', updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    winning_ids,
                )
                if losing_ids:
                    placeholders = ",".join("?" for _ in losing_ids)
                    conn.execute(
                        f"""
                        UPDATE profile_fact_candidates
                        SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})
                        """,
                        losing_ids,
                    )
            elif len(winning_ids) > 1:
                placeholders = ",".join("?" for _ in winning_ids)
                conn.execute(
                    f"""
                    UPDATE profile_fact_candidates
                    SET status = 'merged', updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    winning_ids,
                )

    for user_id in promoted_users:
        try:
            invalidate_profile_context_cache(user_id)
        except Exception:
            pass
    if promoted_users:
        print(f"[nightly] Promoted merged profile facts for {len(promoted_users)} users")


def _load_memory_note_entries(raw_value: object) -> list[dict[str, object]]:
    parsed_object = _safe_json_object(raw_value)
    if parsed_object:
        raw_notes = parsed_object.get("notes", [])
    else:
        raw_notes = _safe_json_array(raw_value)
    notes: list[dict[str, object]] = []
    if isinstance(raw_notes, list):
        for item in raw_notes:
            if isinstance(item, dict):
                notes.append(dict(item))
            else:
                summary = str(item or "").strip()
                if summary:
                    notes.append({"summary": summary})
    return notes


def promote_durable_memories(limit_per_user: int = 8) -> None:
    """Promote repeated/high-signal memories into profile_context memory notes."""

    promoted_users: set[int] = set()
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT
                user_id,
                message_id,
                COALESCE(NULLIF(summary, ''), NULLIF(content, ''), '') AS summary_text,
                importance_score,
                emotional_salience,
                user_value_score,
                reference_count,
                COALESCE(last_referenced_at, created_at) AS updated_at
            FROM conversation_embeddings
            WHERE role = 'user'
              AND (
                    COALESCE(reference_count, 0) >= 2
                 OR (COALESCE(importance_score, 0.0) >= 7.0 AND (
                        COALESCE(user_value_score, 0.0) >= 0.60
                     OR COALESCE(emotional_salience, 0.0) >= 0.55
                 ))
              )
            ORDER BY user_id, COALESCE(reference_count, 0) DESC, COALESCE(importance_score, 0.0) DESC, updated_at DESC
            """
        ).fetchall()

    per_user_notes: dict[int, list[dict[str, object]]] = {}
    seen_per_user: dict[int, set[str]] = {}
    for row in rows:
        user_id = _coerce_int(row["user_id"])
        summary_text = str(row["summary_text"] or "").strip()
        if not summary_text:
            continue
        normalized = _normalize_text_key(summary_text)
        seen = seen_per_user.setdefault(user_id, set())
        if normalized in seen:
            continue
        seen.add(normalized)
        per_user_notes.setdefault(user_id, []).append(
            {
                "summary": summary_text[:200],
                "message_id": _coerce_int(row["message_id"]),
                "importance_score": round(_coerce_float(row["importance_score"]), 2),
                "reference_count": _coerce_int(row["reference_count"]),
                "updated_at": str(row["updated_at"] or operator_now().isoformat()),
                "source": "nightly_memory_promotion",
            }
        )

    with db_rw() as conn:
        for user_id, notes in per_user_notes.items():
            if not notes:
                continue
            row = conn.execute(
                "SELECT value FROM profile_context WHERE user_id = ? AND key = 'memory_notes'",
                (user_id,),
            ).fetchone()
            merged: list[dict[str, object]] = []
            seen = set()
            for item in notes + _load_memory_note_entries(row["value"] if row else None):
                summary = str(item.get("summary") or item.get("note") or "").strip()
                normalized = _normalize_text_key(summary)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                merged.append(
                    {
                        "summary": summary[:200],
                        "message_id": item.get("message_id"),
                        "importance_score": _coerce_float(item.get("importance_score")),
                        "reference_count": _coerce_int(item.get("reference_count")),
                        "updated_at": str(item.get("updated_at") or operator_now().isoformat()),
                        "source": str(item.get("source") or "memory_note"),
                    }
                )
            merged.sort(
                key=lambda item: (
                    _coerce_float(item.get("importance_score")),
                    _coerce_int(item.get("reference_count")),
                    str(item.get("updated_at") or ""),
                ),
                reverse=True,
            )
            payload = json.dumps({"notes": merged[:limit_per_user]}, ensure_ascii=True)
            conn.execute(
                """
                INSERT INTO profile_context (user_id, key, value, created_at, updated_at)
                VALUES (?, 'memory_notes', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, key) DO UPDATE
                SET value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, payload),
            )
            promoted_users.add(user_id)

    for user_id in promoted_users:
        try:
            invalidate_profile_context_cache(user_id)
        except Exception:
            pass
    if promoted_users:
        print(f"[nightly] Promoted durable memories for {len(promoted_users)} users")


def rescore_referenced_memories() -> None:
    """Re-score older memories using later retrieval usage and salience signals."""

    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                importance_score,
                emotional_salience,
                user_value_score,
                context_score,
                reference_count,
                last_referenced_at,
                created_at
            FROM conversation_embeddings
            """
        ).fetchall()

    updates: list[tuple[float, int]] = []
    now = operator_now()
    for row in rows:
        base = _coerce_float(row["importance_score"], 5.0)
        reference_count = max(0, _coerce_int(row["reference_count"]))
        rescored = base + min(1.8, 0.35 * math.log1p(reference_count))
        rescored += 0.35 * _coerce_float(row["emotional_salience"])
        rescored += 0.45 * _coerce_float(row["user_value_score"])
        rescored += 0.20 * _coerce_float(row["context_score"])
        last_referenced_at = row["last_referenced_at"]
        if last_referenced_at:
            try:
                last_ref = datetime.fromisoformat(str(last_referenced_at).replace("Z", "+00:00"))
                if last_ref.tzinfo is None:
                    last_ref = last_ref.replace(tzinfo=now.tzinfo)
                age_days = max(0.0, (now - last_ref).total_seconds() / 86400.0)
                rescored += max(0.0, 0.6 - min(0.6, age_days / 45.0))
            except Exception:
                pass
        updates.append((round(max(0.0, min(10.0, rescored)), 2), _coerce_int(row["id"])))

    if not updates:
        return
    with db_rw() as conn:
        conn.executemany(
            """
            UPDATE conversation_embeddings
            SET importance_score = ?
            WHERE id = ?
            """,
            updates,
        )
    print(f"[nightly] Re-scored {len(updates)} conversation memories")


def repair_turn_audits_from_contradictions() -> None:
    """Mark turn audits that need correction after contradiction evidence emerges."""

    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT
                ta.id,
                ta.plan_json,
                ta.followup_json,
                ta.status,
                GROUP_CONCAT(DISTINCT pfc.key) AS contradiction_keys
            FROM turn_audit_log AS ta
            LEFT JOIN profile_fact_candidates AS pfc
              ON pfc.user_id = ta.user_id
             AND (
                    (ta.correlation_id IS NOT NULL AND pfc.correlation_id = ta.correlation_id)
                 OR (ta.user_message_id IS NOT NULL AND pfc.message_id = ta.user_message_id)
             )
             AND pfc.contradiction = 1
            WHERE ta.created_at >= datetime('now', '-30 days')
            GROUP BY ta.id, ta.plan_json, ta.followup_json, ta.status
            """
        ).fetchall()

    flagged = 0
    for row in rows:
        plan = _safe_json_object(row["plan_json"])
        followup = _safe_json_object(row["followup_json"])
        contradiction_keys = [
            key.strip()
            for key in str(row["contradiction_keys"] or "").split(",")
            if key and key.strip()
        ]
        if not contradiction_keys:
            plan_contradictions = plan.get("contradictions", [])
            if isinstance(plan_contradictions, list):
                contradiction_keys = [
                    str(item.get("key") or "").strip()
                    for item in plan_contradictions
                    if isinstance(item, dict) and str(item.get("key") or "").strip()
                ]
        if not contradiction_keys:
            continue
        review = followup.get("assistant_reply_review", {})
        if isinstance(review, dict) and review.get("is_correction"):
            continue
        followup["repair_recommended"] = True
        followup["repair_reason"] = "profile_contradiction_detected_nightly"
        followup["contradiction_keys"] = contradiction_keys
        followup["repair_created_at"] = operator_now().isoformat()
        updated_audit = False
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    UPDATE turn_audit_log
                    SET followup_json = ?, updated_at = CURRENT_TIMESTAMP, status = 'repair_pending'
                    WHERE id = ?
                    """,
                    (json.dumps(followup, ensure_ascii=True), _coerce_int(row["id"])),
                )
            updated_audit = True
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Failed to flag turn audit {row['id']} for repair: {exc}")
        if updated_audit:
            append_turn_route(
                audit_id=_coerce_int(row["id"]),
                stage="nightly.contradiction_repair_flagged",
                status="repair_pending",
                contradiction_keys=contradiction_keys,
            )
            flagged += 1

    if flagged:
        print(f"[nightly] Flagged {flagged} turn audits for contradiction repair")


def _compute_user_metrics_duckdb(
    con: duckdb.DuckDBPyConnection, user_id: int, telegram_user_id: int
) -> dict:
    cfg = settings()
    transcripts_dir = (
        Path(cfg.data_root) / "users" / str(telegram_user_id) / "transcripts"
    )

    msg_stats = con.execute(
        """
        SELECT
            COUNT(*) AS total_messages,
            COUNT(CASE WHEN role = 'user' THEN 1 END) AS user_messages,
            COUNT(CASE WHEN role = 'assistant' THEN 1 END) AS bot_messages
        FROM sqlite_db.messages
        WHERE user_id = ?
          AND timestamp >= datetime('now', '-30 days')
        """,
        (user_id,),
    ).fetchone()

    sentiment_stats = con.execute(
        """
        SELECT
            AVG(s.valence) AS avg_valence,
            AVG(s.arousal) AS avg_arousal,
            STDDEV(s.valence) AS valence_stddev
        FROM sqlite_db.sentiments AS s
        JOIN sqlite_db.messages AS m ON m.id = s.message_id
        WHERE m.user_id = ?
          AND s.processed_at >= datetime('now', '-30 days')
        """,
        (user_id,),
    ).fetchone()

    daily_activity: list[dict] = []
    if transcripts_dir.exists():
        try:
            rows = con.execute(
                f"""
                SELECT DATE(ts) AS date, COUNT(*) AS message_count
                FROM read_json_auto('{transcripts_dir.as_posix()}/*.jsonl')
                WHERE ts >= current_date - INTERVAL 30 DAYS
                GROUP BY DATE(ts)
                ORDER BY date
                """
            ).fetchall()
            daily_activity = [{"date": str(row[0]), "count": row[1]} for row in rows]
        except Exception as exc:  # noqa: BLE001
            print(f"[nightly] Unable to read transcripts for user {user_id}: {exc}")

    return {
        "user_id": user_id,
        "period": "30_days",
        "message_stats": {
            "total": msg_stats[0] if msg_stats else 0,
            "user": msg_stats[1] if msg_stats else 0,
            "bot": msg_stats[2] if msg_stats else 0,
        },
        "sentiment": {
            "avg_valence": (
                float(sentiment_stats[0])
                if sentiment_stats and sentiment_stats[0] is not None
                else 0.0
            ),
            "avg_arousal": (
                float(sentiment_stats[1])
                if sentiment_stats and sentiment_stats[1] is not None
                else 0.0
            ),
            "valence_stddev": (
                float(sentiment_stats[2])
                if sentiment_stats and sentiment_stats[2] is not None
                else 0.0
            ),
        },
        "daily_activity": daily_activity,
        "generated_at": operator_now().isoformat(),
    }


def scan_for_crisis_indicators(sample: int = 5) -> None:
    with db_ro() as conn:
        users = conn.execute("SELECT id FROM users").fetchall()

    scope_join, scope_predicate = _standard_message_scope_sql("m")
    for user in users:
        user_id = user["id"]
        with db_ro() as conn:
            sentiments = conn.execute(
                f"""
                SELECT s.valence, s.processed_at
                FROM sentiments AS s
                JOIN messages AS m ON m.id = s.message_id
                {scope_join}
                WHERE m.user_id = ?
                  AND {scope_predicate}
                  AND s.processed_at >= datetime('now', '-7 days')
                ORDER BY s.processed_at ASC
                LIMIT ?
                """,
                (user_id, 100),
            ).fetchall()

        if len(sentiments) < sample:
            continue

        valences = [row["valence"] for row in sentiments]
        slope = _trend_slope(valences)
        avg_valence = sum(valences) / len(valences)

        if slope < -0.15 or avg_valence < -0.5:
            with db_rw() as conn:
                existing = conn.execute(
                    """
                    SELECT id
                    FROM moderation_events
                    WHERE user_id = ?
                      AND event_type = 'declining_mood_trend'
                      AND resolved = 0
                      AND timestamp >= datetime('now', '-7 days')
                    """,
                    (user_id,),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """
                        INSERT INTO moderation_events(user_id, event_type, severity, details)
                        VALUES(?, 'declining_mood_trend', 4, ?)
                        """,
                        (
                            user_id,
                            json.dumps(
                                {
                                    "trend_slope": slope,
                                    "avg_valence": avg_valence,
                                    "sample_size": len(valences),
                                }
                            ),
                        ),
                    )
                    print(f"[nightly] Flagged user {user_id} for declining mood trend")


def _trend_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x = list(range(n))
    x_mean = sum(x) / n
    y_mean = sum(values) / n
    numerator = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    denominator = sum((x[i] - x_mean) ** 2 for i in range(n))
    return numerator / denominator if denominator else 0.0


def cleanup_old_data() -> None:
    cfg = settings()
    backup_root = Path(cfg.data_root) / "backups"
    cutoff = operator_now() - timedelta(days=30)
    if backup_root.exists():
        for child in backup_root.iterdir():
            try:
                child_time = datetime.strptime(child.name, "%Y-%m-%d_%H%M%S")
            except ValueError:
                continue
            if child_time < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                print(f"[nightly] Removed old backup {child.name}")

    with db_rw() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET status = 'archived'
            WHERE status = 'active'
              AND last_message_at < datetime('now', '-30 days')
            """,
        )


def run_nightly(hour: int = 3, minute: int = 0) -> None:
    """Blocking scheduler that runs nightly_pipeline at a specific CST time."""

    target = (hour % 24, minute % 60)
    print(
        f"[nightly] Scheduler running; will execute nightly pipeline at {target[0]:02d}:{target[1]:02d} CT"
    )
    while True:
        now = operator_now()
        if now.hour == target[0] and now.minute == target[1]:
            nightly_pipeline()
            time.sleep(60)
        time.sleep(5)


if __name__ == "__main__":
    run_nightly()
