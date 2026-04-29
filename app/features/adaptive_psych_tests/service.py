"""Service layer for adaptive psychological assessments."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from math import isfinite
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.db import db_ro, db_rw
from app.utils.ollama import generate
from app.utils.time_utils import operator_now

_FALLBACK_QUESTIONS = [
    {
        "question": "What is one area of your mental or emotional wellbeing that feels unclear right now?",
        "purpose": "Establish baseline focus",
    },
    {
        "question": "When you are under stress, what are the first thoughts or behaviors that tend to show up?",
        "purpose": "Identify coping patterns",
    },
    {
        "question": "Who or what helps you feel most supported when things are difficult?",
        "purpose": "Surface support network",
    },
]

_DEFAULT_PROFILE_CATEGORIES: Tuple[str, ...] = (
    "mental_health_indicators",
    "psychological_traits",
    "communication_patterns",
    "coping_mechanisms",
    "motivation_drivers",
    "values_priorities",
    "defense_mechanisms",
    "blindspots",
)

_FOCUS_CATEGORY_MAP: Dict[str, Tuple[str, ...]] = {
    "mental": ("mental_health_indicators", "coping_mechanisms", "defense_mechanisms"),
    "anxiety": ("mental_health_indicators", "coping_mechanisms"),
    "mood": ("mental_health_indicators", "coping_mechanisms"),
    "stress": ("coping_mechanisms", "communication_patterns"),
    "relationship": (
        "attachment_style",
        "relationship_patterns",
        "support_network",
        "coping_mechanisms",
    ),
    "personality": (
        "psychological_traits",
        "motivation_drivers",
        "communication_patterns",
        "dark_triad",
    ),
    "career": ("values_priorities", "motivation_drivers", "cognitive_metrics"),
    "identity": ("values_priorities", "blindspots"),
    "self_care": ("coping_mechanisms", "therapeutic_recommendations"),
}

_LOW_CONF_THRESHOLD = 0.65
_RECENT_MESSAGE_LIMIT = 8
_RECENT_QUESTION_LIMIT = 25
_MAX_GAP_ITEMS = 12
_QUALITATIVE_NOTE_LIMIT = 5


@dataclass
class AssessmentStep:
    index: int
    question: str
    is_final: bool


@dataclass
class AssessmentResult:
    summary: str
    profile_delta: Optional[dict] = None


class ProfileAssessmentManager:
    """Create and track adaptive psych assessment sessions."""

    def __init__(self, log_callback):
        self._log = log_callback

    def start_session(
        self, user_id: int, focus_area: Optional[str] = None
    ) -> AssessmentStep:
        """Create a new session and return the first question."""

        session_id = uuid.uuid4().hex
        questions = self._generate_questions(user_id, focus_area)
        payload = json.dumps(questions)
        with db_rw() as conn:
            conn.execute(
                """
                INSERT INTO profile_assessment_sessions (id, user_id, focus_area, question_data)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, user_id, focus_area or "comprehensive", payload),
            )
        question = questions[0]["question"]
        return AssessmentStep(index=0, question=question, is_final=len(questions) == 1)

    def get_active_session(self, user_id: int) -> Optional[dict]:
        with db_ro() as conn:
            row = conn.execute(
                """
                SELECT * FROM profile_assessment_sessions
                WHERE user_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_response(self, session: dict, answer: str):
        session_id = session["id"]
        user_id = session["user_id"]
        questions = json.loads(session["question_data"])
        index = int(session["current_index"])
        question = questions[index]["question"]
        completed = False
        with db_rw() as conn:
            conn.execute(
                """
                INSERT INTO profile_assessment_responses (session_id, question_index, question, response)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, index, question, answer),
            )
            next_index = index + 1
            if next_index >= len(questions):
                conn.execute(
                    """
                    UPDATE profile_assessment_sessions
                    SET status = 'completed', current_index = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_index, _now(), session_id),
                )
                completed = True
            else:
                conn.execute(
                    """
                    UPDATE profile_assessment_sessions
                    SET current_index = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_index, _now(), session_id),
                )

        session["current_index"] = next_index
        if completed:
            qa_pairs = self._fetch_session_responses(session_id)
            summary = self._summarize_session(session_id, user_id, qa_pairs)
            profile_delta = self._integrate_assessment_results(
                user_id=user_id,
                session=session,
                qa_pairs=qa_pairs,
                summary=summary,
            )
            return AssessmentResult(summary=summary, profile_delta=profile_delta)

        next_question = questions[next_index]["question"]
        is_final = next_index == len(questions) - 1
        return AssessmentStep(
            index=next_index, question=next_question, is_final=is_final
        )

    def cancel_session(self, session_id: str) -> None:
        with db_rw() as conn:
            conn.execute(
                """
                UPDATE profile_assessment_sessions
                SET status = 'cancelled', updated_at = ?
                WHERE id = ?
                """,
                (_now(), session_id),
            )

    # ------------------------------------------------------------------
    # Internals

    def _generate_questions(
        self, user_id: int, focus_area: Optional[str]
    ) -> List[dict]:
        profile, _profile_meta, recent_messages, recent_questions = (
            self._load_question_context(user_id)
        )
        gaps = self._identify_profile_gaps(profile, focus_area)
        profile_snapshot = self._extract_profile_snapshot(profile, gaps)

        prompt = self._build_prompt(
            focus_area=focus_area,
            gaps=gaps,
            profile_snapshot=profile_snapshot,
            recent_messages=recent_messages,
            prior_questions=recent_questions,
        )
        try:
            result = generate(prompt, format="json", options={"temperature": 0.45})
            questions = json.loads(result.get("text", "[]"))
            if isinstance(questions, list) and questions:
                normalized: List[dict] = []
                for item in questions:
                    normalized_item = self._normalize_question(item)
                    if normalized_item.get("question"):
                        normalized.append(normalized_item)
                    if len(normalized) >= 10:
                        break
                if normalized:
                    return normalized
        except Exception as exc:  # noqa: BLE001
            self._log(f"Adaptive psych question generation failed: {exc}")
        return _FALLBACK_QUESTIONS

    def _normalize_question(self, item) -> dict:
        if isinstance(item, dict):
            question = item.get("question") or item.get("question_text") or ""
            question_text = str(question).strip()
            normalized = {
                "question": question_text,
                "purpose": str(item.get("purpose", "")).strip(),
            }
            for key in (
                "category",
                "focus_area",
                "targets",
                "focus_tags",
                "tone",
                "follow_up_hint",
            ):
                if key in item:
                    normalized[key] = item[key]
            return normalized
        if isinstance(item, str):
            return {"question": item, "purpose": ""}
        return {"question": str(item), "purpose": ""}

    def _build_prompt(
        self,
        focus_area: Optional[str],
        gaps: Sequence[dict],
        profile_snapshot: Sequence[dict],
        recent_messages: Sequence[str],
        prior_questions: Sequence[str],
    ) -> str:
        focus = focus_area or "comprehensive"
        gap_payload = json.dumps(
            list(gaps)[:_MAX_GAP_ITEMS], indent=2, ensure_ascii=True
        )
        snapshot_payload = json.dumps(
            list(profile_snapshot)[:_MAX_GAP_ITEMS], indent=2, ensure_ascii=True
        )
        messages_payload = json.dumps(
            list(recent_messages)[:_RECENT_MESSAGE_LIMIT], indent=2, ensure_ascii=True
        )
        prior_payload = json.dumps(
            list(prior_questions)[:_RECENT_QUESTION_LIMIT], indent=2, ensure_ascii=True
        )

        return (
            "You are an adaptive psychology intake strategist. "
            "Design the next set of targeted, conversational questions to deepen the user's psychological profile.\n\n"
            f"FOCUS AREA: {focus}\n"
            "PROFILE GAPS (highest uncertainty first):\n"
            f"{gap_payload}\n\n"
            "CURRENT PROFILE SNAPSHOT (values & confidence):\n"
            f"{snapshot_payload}\n\n"
            "RECENT USER MESSAGES (most recent first):\n"
            f"{messages_payload}\n\n"
            "QUESTIONS ALREADY ASKED RECENTLY (avoid repeating themes or wording):\n"
            f"{prior_payload}\n\n"
            "Return a JSON array with EXACTLY 5 objects. Each object must include:\n"
            "  - question: conversational sentence prompting open reflection (no yes/no)\n"
            "  - purpose: short description of the insight sought\n"
            "  - category: short label (e.g., mental_health, relationships, values)\n"
            "  - focus_tags: array with up to 3 keywords highlighting the angle (optional but recommended)\n"
            "  - tone: one of ['gentle','curious','direct'] adapted to the content\n"
            "At least one question should feel lighter/supportive if the rest are heavy. "
            "Build gentle rapport while still addressing the gaps. "
            "Do not include markdown, numbering, or commentary outside the JSON array."
        )

    def _load_question_context(
        self, user_id: int
    ) -> Tuple[dict, Optional[dict], List[str], List[str]]:
        profile: dict = {}
        profile_meta: Optional[dict] = None
        recent_messages: List[str] = []
        prior_questions: List[str] = []

        with db_ro() as conn:
            profile_row = conn.execute(
                """
                SELECT id, profile_data, messages_analyzed, created_at
                FROM psychological_profiles
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if profile_row:
                profile_meta = dict(profile_row)
                try:
                    profile = json.loads(profile_row["profile_data"] or "{}")
                except json.JSONDecodeError:
                    self._log(
                        f"Invalid stored profile JSON for user {user_id}; using empty profile"
                    )
                    profile = {}

            message_rows = conn.execute(
                """
                SELECT content
                FROM messages
                WHERE user_id = ? AND role = 'user' AND COALESCE(scope, 'standard') = 'standard'
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, _RECENT_MESSAGE_LIMIT),
            ).fetchall()
            recent_messages = [row["content"] for row in message_rows if row["content"]]

            question_rows = conn.execute(
                """
                SELECT question
                FROM profile_assessment_responses
                WHERE session_id IN (
                    SELECT id FROM profile_assessment_sessions
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 6
                )
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, _RECENT_QUESTION_LIMIT),
            ).fetchall()
            prior_questions = [
                row["question"] for row in question_rows if row["question"]
            ]

        return profile, profile_meta, recent_messages, prior_questions

    def _fetch_session_responses(self, session_id: str) -> List[dict]:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT question_index, question, response
                FROM profile_assessment_responses
                WHERE session_id = ?
                ORDER BY question_index ASC
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "index": row["question_index"],
                "question": row["question"],
                "response": row["response"],
            }
            for row in rows
        ]

    def _identify_profile_gaps(
        self, profile: dict, focus_area: Optional[str]
    ) -> List[dict]:
        categories = self._resolve_focus_categories(focus_area)
        gaps: List[dict] = []

        if not isinstance(profile, dict) or not profile:
            return [
                {
                    "path": category,
                    "category": category,
                    "confidence": 0.0,
                    "reason": "no existing data",
                }
                for category in categories[:_MAX_GAP_ITEMS]
            ]

        seen: set[str] = set()
        for category in categories:
            if len(gaps) >= _MAX_GAP_ITEMS:
                break
            seen.add(category)
            if category not in profile:
                gaps.append(
                    {
                        "path": category,
                        "category": category,
                        "confidence": 0.0,
                        "reason": "missing category",
                    }
                )
                continue
            self._collect_low_confidence_metrics(
                destination=gaps,
                category=category,
                data=profile[category],
                prefix=category,
            )

        # Capture any remaining default categories that are fully missing
        if len(gaps) < _MAX_GAP_ITEMS:
            for category in _DEFAULT_PROFILE_CATEGORIES:
                if len(gaps) >= _MAX_GAP_ITEMS:
                    break
                if category in seen:
                    continue
                if category not in profile:
                    gaps.append(
                        {
                            "path": category,
                            "category": category,
                            "confidence": 0.0,
                            "reason": "missing category",
                        }
                    )

        return gaps[:_MAX_GAP_ITEMS]

    def _collect_low_confidence_metrics(
        self,
        destination: List[dict],
        category: str,
        data: Any,
        prefix: str,
    ) -> None:
        if len(destination) >= _MAX_GAP_ITEMS:
            return

        if isinstance(data, dict) and "value" in data and "confidence" in data:
            confidence = _coerce_confidence(data.get("confidence"))
            if confidence < _LOW_CONF_THRESHOLD:
                destination.append(
                    {
                        "path": prefix,
                        "category": category,
                        "confidence": confidence,
                        "value": _safe_float(data.get("value")),
                        "reason": "low confidence metric",
                    }
                )
            return

        if isinstance(data, dict):
            for key, value in data.items():
                if len(destination) >= _MAX_GAP_ITEMS:
                    break
                child_prefix = f"{prefix}.{key}" if prefix else key
                self._collect_low_confidence_metrics(
                    destination, category, value, child_prefix
                )
            return

        if isinstance(data, list):
            if not data:
                destination.append(
                    {
                        "path": prefix,
                        "category": category,
                        "confidence": 0.0,
                        "reason": "no entries collected yet",
                    }
                )
            return

    def _resolve_focus_categories(self, focus_area: Optional[str]) -> Tuple[str, ...]:
        if not focus_area:
            return _DEFAULT_PROFILE_CATEGORIES

        normalized = focus_area.lower().strip()
        categories: List[str] = []

        for key, mapped in _FOCUS_CATEGORY_MAP.items():
            if key in normalized:
                categories.extend(mapped)

        if not categories:
            categories.extend(_DEFAULT_PROFILE_CATEGORIES)

        # Deduplicate while preserving order
        seen = set()
        ordered = []
        for item in categories:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return tuple(ordered)

    def _extract_profile_snapshot(
        self, profile: dict, gaps: Sequence[dict]
    ) -> List[dict]:
        snapshot: List[dict] = []
        if not isinstance(profile, dict):
            return snapshot

        for gap in gaps:
            if len(snapshot) >= _MAX_GAP_ITEMS:
                break
            path = gap.get("path")
            if not path:
                continue
            node = self._get_nested(profile, path.split("."))
            if isinstance(node, dict):
                snapshot.append(
                    {
                        "path": path,
                        "value": node.get("value"),
                        "confidence": node.get("confidence"),
                        "updated_at": node.get("updated_at"),
                    }
                )
            elif isinstance(node, list):
                snapshot.append(
                    {
                        "path": path,
                        "list_length": len(node),
                    }
                )

        return snapshot

    def _get_nested(self, data: dict, path: Sequence[str]) -> Any:
        node: Any = data
        for key in path:
            if not key:
                continue
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    def _load_profile_data(self, user_id: int) -> Tuple[dict, Optional[dict]]:
        with db_ro() as conn:
            row = conn.execute(
                """
                SELECT id, profile_data, messages_analyzed, created_at
                FROM psychological_profiles
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return {}, None
        profile_meta = dict(row)
        try:
            profile = json.loads(row["profile_data"] or "{}")
        except json.JSONDecodeError:
            self._log(
                f"Invalid stored profile JSON for user {user_id}; using empty profile"
            )
            profile = {}
        return profile, profile_meta

    def _integrate_assessment_results(
        self,
        user_id: int,
        session: dict,
        qa_pairs: Sequence[dict],
        summary: str,
    ) -> dict:
        focus_area = session.get("focus_area") or "comprehensive"
        profile, profile_meta = self._load_profile_data(user_id)
        gaps = self._identify_profile_gaps(profile, focus_area)
        profile_snapshot = self._extract_profile_snapshot(profile, gaps)

        updates: Optional[dict] = None
        try:
            updates = self._generate_profile_updates(
                profile_snapshot=profile_snapshot,
                qa_pairs=qa_pairs,
                summary=summary,
                focus_area=focus_area,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"Adaptive psych profile update generation failed: {exc}")

        try:
            profile_delta = self._merge_profile_updates(
                profile=profile,
                updates=updates,
                session_id=session["id"],
                focus_area=focus_area,
                summary=summary,
                qa_pairs=qa_pairs,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"Adaptive psych profile merge failed: {exc}")
            profile_delta = {
                "metrics_updated": 0,
                "list_items_added": 0,
                "qualitative_notes_recorded": 0,
            }

        messages_analyzed = self._estimate_messages_analyzed(user_id, profile_meta)
        self._persist_profile(user_id, profile, messages_analyzed)
        return profile_delta

    def _generate_profile_updates(
        self,
        profile_snapshot: Sequence[dict],
        qa_pairs: Sequence[dict],
        summary: str,
        focus_area: str,
    ) -> Optional[dict]:
        context = {
            "focus_area": focus_area,
            "profile_snapshot": list(profile_snapshot)[:_MAX_GAP_ITEMS],
            "qa_pairs": [
                {"question": pair["question"], "response": pair["response"]}
                for pair in list(qa_pairs)[:10]
            ],
            "session_summary": summary,
        }
        prompt = (
            "You are assisting with a psychological profile. "
            "Translate the short adaptive assessment below into incremental profile updates. "
            "Only propose updates when the evidence is meaningful.\n\n"
            "Return JSON with the following structure:\n"
            "{\n"
            '  "metric_updates": [\n'
            "    {\n"
            '      "category": "<existing category name>",\n'
            '      "metric": "<metric within the category>",\n'
            '      "new_value": <numeric value>,\n'
            '      "confidence": <0.0-1.0 confidence in this adjustment>,\n'
            '      "evidence": "short quote or rationale"\n'
            "    }\n"
            "  ],\n"
            '  "list_additions": [\n'
            "    {\n"
            '      "category": "<list-type category>",\n'
            '      "items": ["new insight 1", "new insight 2"]\n'
            "    }\n"
            "  ],\n"
            '  "qualitative_insights": ["short free-text takeaways to store alongside the session"]\n'
            "}\n\n"
            "Allow at most 5 metric_updates and 5 list additions. "
            "Favour categories that appear in the profile snapshot or align with the focus area. "
            "Do not invent new categories. "
            "Keep insights concise and evidence grounded in the user's responses."
        )
        result = generate(
            prompt=f"{prompt}\n\nCONTEXT:\n{json.dumps(context, indent=2, ensure_ascii=True)}",
            format="json",
            options={"temperature": 0.2},
        )
        raw = result.get("text", "{}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._log(f"Adaptive assessment profile update JSON parse failed: {exc}")
            return {}

    def _merge_profile_updates(
        self,
        profile: dict,
        updates: Optional[dict],
        session_id: str,
        focus_area: str,
        summary: str,
        qa_pairs: Sequence[dict],
    ) -> dict:
        if not isinstance(profile, dict):
            profile = {}

        metrics_updated = 0
        list_items_added = 0
        qualitative_insights: list[str] = []
        qa_list = list(qa_pairs)

        metric_updates = (updates or {}).get("metric_updates", []) if updates else []
        for entry in metric_updates[:5]:
            category = entry.get("category")
            metric = entry.get("metric")
            new_value = entry.get("new_value")
            confidence = entry.get("confidence")
            evidence = entry.get("evidence")

            if not category or not metric:
                continue

            target = profile.setdefault(category, {})
            if not isinstance(target, dict):
                continue

            existing = target.get(metric, {})
            existing_value = None
            existing_conf = 0.0
            if isinstance(existing, dict):
                existing_value = existing.get("value")
                existing_conf = _coerce_confidence(existing.get("confidence"))
            elif isinstance(existing, (int, float)):
                existing_value = float(existing)

            incoming_value = _safe_float(new_value, default=existing_value)
            incoming_conf = _coerce_confidence(confidence)
            if incoming_value is None or incoming_conf <= 0.0:
                continue

            combined_value, combined_conf = _combine_metric_values(
                existing_value, existing_conf, incoming_value, incoming_conf
            )
            entry_record = dict(existing) if isinstance(existing, dict) else {}
            entry_record.update(
                {
                    "value": combined_value,
                    "confidence": combined_conf,
                    "updated_at": _now(),
                    "last_source": "adaptive_assessment",
                    "last_session_id": session_id,
                }
            )
            if evidence:
                evidence_list = entry_record.get("evidence")
                if isinstance(evidence_list, list):
                    pass
                elif evidence_list:
                    evidence_list = [evidence_list]
                else:
                    evidence_list = []
                evidence_list.append(
                    {
                        "note": evidence,
                        "session_id": session_id,
                        "captured_at": _now(),
                    }
                )
                entry_record["evidence"] = evidence_list
            target[metric] = entry_record
            metrics_updated += 1

        list_additions = (updates or {}).get("list_additions", []) if updates else []
        for addition in list_additions[:5]:
            category = addition.get("category")
            items = addition.get("items") or []
            if not category or not items:
                continue
            target_list = profile.setdefault(category, [])
            if not isinstance(target_list, list):
                continue
            added = 0
            for item in items:
                if item and item not in target_list:
                    target_list.append(item)
                    added += 1
            list_items_added += added

        qualitative_insights = (updates or {}).get("qualitative_insights") or []

        adaptive_section = profile.setdefault("adaptive_assessments", {})
        history = adaptive_section.get("history") or []
        history = [entry for entry in history if entry.get("session_id") != session_id]
        history.append(
            {
                "session_id": session_id,
                "focus_area": focus_area,
                "summary": summary,
                "insights": qualitative_insights[:_QUALITATIVE_NOTE_LIMIT],
                "completed_at": _now(),
                "question_count": len(qa_list),
            }
        )
        # Keep only the most recent 10 sessions
        adaptive_section["history"] = history[-10:]
        adaptive_section["last_summary"] = summary
        adaptive_section["last_focus_area"] = focus_area
        adaptive_section["last_completed_at"] = _now()
        adaptive_section["last_session_id"] = session_id
        adaptive_section["last_question_count"] = len(qa_list)

        return {
            "metrics_updated": metrics_updated,
            "list_items_added": list_items_added,
            "qualitative_notes_recorded": len(qualitative_insights),
        }

    def _estimate_messages_analyzed(
        self, user_id: int, profile_meta: Optional[dict]
    ) -> int:
        if profile_meta:
            raw = profile_meta.get("messages_analyzed")
            try:
                if raw is not None:
                    return int(raw)
            except (TypeError, ValueError):
                pass
        with db_ro() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM messages
                WHERE user_id = ? AND role = 'user' AND COALESCE(scope, 'standard') = 'standard'
                """,
                (user_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def _persist_profile(
        self, user_id: int, profile: dict, messages_analyzed: int
    ) -> None:
        with db_rw() as conn:
            conn.execute(
                """
                INSERT INTO psychological_profiles (user_id, profile_data, messages_analyzed)
                VALUES (?, ?, ?)
                """,
                (user_id, json.dumps(profile), messages_analyzed),
            )

    def _summarize_session(
        self, session_id: str, user_id: int, qa_pairs: Sequence[dict]
    ) -> str:
        if not qa_pairs:
            return "Assessment responses were recorded, but no content was available to summarize."

        trimmed_pairs = [
            {"question": pair["question"], "response": pair["response"]}
            for pair in list(qa_pairs)[:10]
        ]
        prompt = (
            "You are summarizing a short adaptive psychology intake session.\n"
            "Create a concise paragraph (<= 120 words) that captures the user's key themes, concerns, "
            "and strengths from the answers below. Highlight patterns, coping strategies, and support systems. "
            "Avoid clinician jargon; be empathetic and plainspoken.\n\n"
            f"RESPONSES:\n{json.dumps(trimmed_pairs, indent=2)}\n"
        )
        summary = ""
        try:
            result = generate(prompt)
            summary = result.get("text", "").strip()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Adaptive psych summary generation failed: {exc}")
            summary = "\n".join(
                f"- {pair['question']} :: {pair['response']}" for pair in trimmed_pairs
            )

        payload = json.dumps(
            {
                "summary": summary,
                "created_at": _now(),
                "session_id": session_id,
            }
        )

        with db_rw() as conn:
            conn.execute(
                """
                INSERT INTO profile_context (user_id, key, value)
                VALUES (?, 'latest_assessment_summary', ?)
                ON CONFLICT(user_id, key)
                DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, payload),
            )
        return summary


def _now() -> str:
    return operator_now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return default
    return val if isfinite(val) else default


def _coerce_confidence(value: Any) -> float:
    conf = _safe_float(value, default=0.0)
    if conf is None:
        conf = 0.0
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


def _combine_metric_values(
    existing_value: Optional[float],
    existing_conf: float,
    new_value: float,
    new_conf: float,
) -> Tuple[float, float]:
    current_value = _safe_float(existing_value, default=None)
    incoming_value = _safe_float(new_value, default=None)
    if incoming_value is None:
        # Nothing meaningful to add; return existing values
        return current_value or 0.0, _coerce_confidence(existing_conf)

    current_conf = _coerce_confidence(existing_conf)
    incoming_conf = _coerce_confidence(new_conf)

    if current_value is None or current_conf <= 0.01:
        return incoming_value, incoming_conf

    if incoming_conf <= 0.0:
        return current_value, current_conf

    combined_value = (
        (current_value * current_conf) + (incoming_value * incoming_conf)
    ) / (current_conf + incoming_conf)
    combined_conf = max(current_conf, min(1.0, current_conf + incoming_conf * 0.5))
    return combined_value, combined_conf
