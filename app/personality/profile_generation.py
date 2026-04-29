"""Mission Statement:
This module powers the wellness bot's personalization engine by translating rich
dialogue history into structured psychological profiles. The goal is to unlock
trustworthy, repeatable insights that enhance adaptive conversations, guide
therapeutic recommendations, and inform long-term care planning. We achieve
this by centralizing prompt construction, LLM orchestration, and post-processing
safeguards so every subsystem can rely on a single source of truth for
comprehensive psych data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict

from app.utils.ollama import generate

logger = logging.getLogger(__name__)

# Constants help keep prompts within safe context windows.
MAX_CONTEXT_CHARS = 70_000
DEFAULT_TEMPERATURE = 0.15
MIN_MESSAGES_FOR_ANALYSIS = 20


class ProfileGenerationError(RuntimeError):
    """Raised when the profile generator cannot produce structured output."""


@dataclass(frozen=True)
class ProfileGenerationResult:
    """Container for profile data and metadata returned by the generator."""

    profile: Dict[str, Any]
    raw_text: str
    message_count: int
    messages_needed_for_95_confidence: int


def generate_comprehensive_profile(
    conversation_sample: str,
    message_count: int,
    *,
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
) -> ProfileGenerationResult:
    """Generate an exhaustive psychological profile from recent conversation text.

    Args:
        conversation_sample: Recent user messages joined into a single string.
        message_count: Number of user-authored messages considered for analysis.
        model: Optional explicit model identifier. Defaults to configured chat model.
        temperature: Decoding temperature passed to the LLM.

    Returns:
        ProfileGenerationResult containing parsed JSON data and raw model text.

    Raises:
        ProfileGenerationError: If the LLM call fails or returns invalid JSON.
    """

    trimmed_sample = conversation_sample.strip()[:MAX_CONTEXT_CHARS]
    if len(trimmed_sample) < 10:
        raise ProfileGenerationError("Conversation sample too small to profile.")
    if message_count < MIN_MESSAGES_FOR_ANALYSIS:
        raise ProfileGenerationError(
            f"At least {MIN_MESSAGES_FOR_ANALYSIS} messages are required; received {message_count}."
        )

    messages_needed_for_95_confidence = max(0, 200 - message_count)
    prompt = _build_profile_prompt(
        trimmed_sample, message_count, messages_needed_for_95_confidence
    )

    try:
        response = generate(
            prompt=prompt,
            model=model,
            format="json",
            options={"temperature": temperature},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Psych profile generation request failed: %s", exc)
        raise ProfileGenerationError("LLM generation failed") from exc

    raw_text = (response.get("text") or "").strip()
    if not raw_text:
        raise ProfileGenerationError("LLM returned empty response")

    json_blob = _extract_json_blob(raw_text)
    try:
        profile: Dict[str, Any] = json.loads(json_blob)
    except json.JSONDecodeError as exc:  # noqa: BLE001
        logger.error("Psych profile JSON parse error: %s", exc)
        logger.debug("Raw profile output (first 500 chars): %s", raw_text[:500])
        raise ProfileGenerationError("LLM returned invalid JSON") from exc

    _ensure_profile_defaults(profile, message_count, messages_needed_for_95_confidence)
    return ProfileGenerationResult(
        profile=profile,
        raw_text=raw_text,
        message_count=message_count,
        messages_needed_for_95_confidence=messages_needed_for_95_confidence,
    )


def _build_profile_prompt(
    conversation_sample: str,
    message_count: int,
    messages_needed_for_95_confidence: int,
) -> str:
    """Construct the comprehensive prompt shared by GUI and nightly workers."""

    header = (
        "Analyze this user's conversation patterns for EXHAUSTIVE psychological profiling. "
        "Be objective, clinical, thorough, and provide confidence scores for each metric. "
        f"You have analyzed {message_count} messages."
    )

    prompt = f"""{header}

Recent Messages:
{conversation_sample}

IMPORTANT: For EVERY numeric metric, provide BOTH the value AND a confidence score (0.0-1.0) in format: {{"value": X, "confidence": Y}}

Output format:
{{
    "executive_summary": {{
        "overview": "<2-3 paragraph narrative summary of the individual's psychological profile>",
        "most_prominent_traits": [<array of 5-7 most defining characteristics>],
        "core_strengths": [<array of 3-5 biggest psychological strengths with examples>],
        "core_weaknesses": [<array of 3-5 biggest limitations/challenges with examples>],
        "overall_functioning": "<assessment of psychological health and functioning>",
        "therapeutic_recommendations": [<array of specific therapeutic approaches that would benefit this person>],
        "messages_analyzed": {message_count},
        "estimated_messages_for_95_confidence": {messages_needed_for_95_confidence}
    }},
    "mental_health_indicators": {{
        "depression_likelihood": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "anxiety_likelihood": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "bipolar_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "adhd_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "ocd_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "ptsd_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "social_anxiety": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "eating_disorder_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "dissociation_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "body_dysmorphia_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "substance_use_risk": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "addiction_vulnerability": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "autism_spectrum_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "personality_typing": {{
        "myers_briggs": {{
            "type": "<INTJ|ENFP|etc - all 16 types>",
            "confidence": <0.0-1.0>,
            "dimensions": {{
                "introversion_extraversion": {{"score": <-1.0 to 1.0, negative=I, positive=E>, "confidence": <0.0-1.0>}},
                "sensing_intuition": {{"score": <-1.0 to 1.0, negative=S, positive=N>, "confidence": <0.0-1.0>}},
                "thinking_feeling": {{"score": <-1.0 to 1.0, negative=T, positive=F>, "confidence": <0.0-1.0>}},
                "judging_perceiving": {{"score": <-1.0 to 1.0, negative=J, positive=P>, "confidence": <0.0-1.0>}}
            }}
        }},
        "enneagram": {{
            "primary_type": <1-9>,
            "wing": "<w1|w2|balanced>",
            "confidence": <0.0-1.0>,
            "integration_direction": <type number when healthy>,
            "disintegration_direction": <type number when stressed>,
            "instinctual_variant": "<sp|sx|so or combination>"
        }},
        "introversion_level": {{"value": <0.0-1.0, 0=extreme extravert, 1=extreme introvert>, "confidence": <0.0-1.0>}}
    }},
    "dark_triad": {{
        "narcissism": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "machiavellianism": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "psychopathy": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "big_five": {{
        "openness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "conscientiousness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "extraversion": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "agreeableness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "neuroticism": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "emotional_intelligence": {{
        "self_awareness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "self_regulation": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "motivation": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "empathy": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "social_skills": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "cognitive_metrics": {{
        "estimated_iq": {{"value": <50-180>, "confidence": <0.0-1.0>}},
        "vocabulary_complexity": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "logical_coherence": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "abstract_thinking": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "creativity_indicators": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "attachment_style": {{
        "primary_type": "<secure|anxious-preoccupied|dismissive-avoidant|fearful-avoidant|earned-secure|disorganized>",
        "confidence": <0.0-1.0>,
        "security_score": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "anxiety_dimension": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "avoidance_dimension": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "disorganization_level": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "cognitive_distortions": {{
        "all_or_nothing_thinking": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "overgeneralization": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "catastrophizing": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "personalization": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "should_statements": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "mental_filtering": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "mind_reading": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "fortune_telling": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "defense_mechanisms": {{
        "mature_adaptive": [<array of adaptive mechanisms with brief rationale>],
        "neurotic_intermediate": [<array of intermediate mechanisms with brief rationale>],
        "immature_maladaptive": [<array of maladaptive mechanisms with brief rationale>],
        "primary_mechanisms": [<array of 3-5 most frequently used mechanisms>]
    }},
    "motivation_drivers": {{
        "achievement": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "affiliation": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "power": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "autonomy": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "learning_style": {{
        "primary": "<visual|auditory|kinesthetic|reading-writing|multimodal>",
        "confidence": <0.0-1.0>
    }},
    "conflict_resolution_style": {{
        "primary": "<competing|collaborating|compromising|avoiding|accommodating>",
        "confidence": <0.0-1.0>
    }},
    "time_perspective": {{
        "past_focus": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "present_focus": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "future_focus": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "locus_of_control": {{"value": <0.0-1.0, 0=external, 1=internal>, "confidence": <0.0-1.0>}},
    "growth_mindset": {{"value": <0.0-1.0, 0=fixed, 1=growth>, "confidence": <0.0-1.0>}},
    "risk_tolerance": {{"value": <0.0-1.0, 0=risk-averse, 1=risk-seeking>, "confidence": <0.0-1.0>}},
    "decision_making_style": {{
        "primary": "<analytical|intuitive|dependent|avoidant|spontaneous>",
        "confidence": <0.0-1.0>
    }},
    "psychological_traits": {{
        "impulsivity": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "resilience": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "self_esteem": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "perfectionism": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "assertiveness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "optimism": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "emotional_stability": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "open_mindedness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "communication_patterns": {{
        "verbosity": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "emotional_expressiveness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "humor_usage": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "formality_level": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}},
        "directness": {{"value": <0.0-1.0>, "confidence": <0.0-1.0>}}
    }},
    "therapeutic_recommendations": {{
        "suggested_treatments": [<array of evidence-based treatment ideas tailored to this user>],
        "therapy_modalities": [<array of modalities like CBT, DBT, ACT, psychodynamic>],
        "handling_strategies": [<array of coaching strategies for the bot to apply>],
        "communication_tips": [<array of ways the bot should speak to build trust>],
        "sensitive_topics": [<array of topics to approach carefully>],
        "strengths_to_leverage": [<array of traits to harness during interventions>]
    }},
    "ideal_partner_profile": {{
        "personality_traits": [<array of supportive traits in a partner>],
        "communication_style": "<brief description>",
        "values_alignment": [<array of core values that must align>],
        "attachment_compatibility": "<best fitting attachment style>",
        "deal_breakers": [<array of absolute no-gos>]
    }},
    "career_recommendations": {{
        "suitable_roles": [<array of well-matched jobs>],
        "work_environment": "<ideal workplace characteristics>",
        "skills_to_develop": [<array of growth areas>],
        "career_values": [<array of values to prioritize at work>]
    }},
    "important_insights": {{
        "user_should_know": [<array of compassionate but honest reflections>],
        "how_to_communicate": [<array of phrasing guidance for delicate points>],
        "timing_considerations": [<array of when to surface these insights>]
    }},
    "blindspots": [<array of significant self-awareness gaps>],
    "idiosyncrasies": [<array of notable quirks, habits, speech patterns>],
    "interests_topics": [<array of main topics user discusses>],
    "coping_mechanisms": [<array of how user handles stress>],
    "notable_strengths": [<array of identified psychological strengths>],
    "areas_for_growth": [<array of potential development areas>]
}}"""
    # TODO: Incorporate personality-mode weighting so the prompt can highlight context-specific nuances.
    return prompt


def _extract_json_blob(raw_text: str) -> str:
    """Extract the JSON object from raw LLM output, tolerating code fences."""

    if "{" not in raw_text or "}" not in raw_text:
        raise ProfileGenerationError("Profile output missing JSON object")

    start = raw_text.find("{")
    end = raw_text.rfind("}") + 1
    candidate = raw_text[start:end]
    return candidate.strip()


def _ensure_profile_defaults(
    profile: Dict[str, Any],
    message_count: int,
    messages_needed_for_95_confidence: int,
) -> None:
    """Back-fill required keys so downstream rendering never explodes."""

    summary = profile.setdefault("executive_summary", {})
    summary.setdefault("messages_analyzed", message_count)
    summary.setdefault(
        "estimated_messages_for_95_confidence", messages_needed_for_95_confidence
    )
    summary.setdefault("therapeutic_recommendations", [])

    for key in (
        "mental_health_indicators",
        "personality_typing",
        "dark_triad",
        "big_five",
        "emotional_intelligence",
        "cognitive_metrics",
        "attachment_style",
        "cognitive_distortions",
        "defense_mechanisms",
        "motivation_drivers",
        "psychological_traits",
        "communication_patterns",
    ):
        profile.setdefault(key, {})

    profile.setdefault("therapeutic_recommendations", {})
    profile.setdefault("ideal_partner_profile", {})
    profile.setdefault("career_recommendations", {})
    profile.setdefault("important_insights", {})

    for list_key in (
        "blindspots",
        "idiosyncrasies",
        "interests_topics",
        "coping_mechanisms",
        "notable_strengths",
        "areas_for_growth",
    ):
        profile.setdefault(list_key, [])

    # TODO: Persist per-metric confidence audits so we can trace how the model justified each figure.
