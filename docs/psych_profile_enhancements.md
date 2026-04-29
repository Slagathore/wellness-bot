# Psychological Profile Enhancements - Exhaustive Update

## Overview

This document describes the comprehensive psychological profile expansion completed on the wellness bot. The profile now includes 200+ metrics across 20+ categories, all with confidence scores.

**2025-10-15 Update:** The nightly worker and admin UI now delegate profile creation to `app/personality/profile_generation.py`, ensuring both surfaces consume the same JSON schema and fail-safes.

## What Was Added

### 1. **Executive Summary** (NEW!)

- Overview paragraph summarizing the individual
- Most prominent traits (top 3-5)
- Core strengths (top 3-5)
- Core challenges/weaknesses (top 3-5)
- Overall functioning assessment
- Therapeutic recommendations (3-5 specific suggestions)
- Messages analyzed count
- Estimated messages needed for 95% confidence

### 2. **Personality Typing Systems** (NEW!)

#### Myers-Briggs Type Indicator (MBTI)

- **Final Type**: 16 personality types (INTJ, ENFP, etc.) with confidence
- **4 Dimensions** (each with score and confidence):
  - Extraversion ↔ Introversion
  - Sensing ↔ Intuition
  - Thinking ↔ Feeling
  - Judging ↔ Perceiving

#### Enneagram

- **Primary Type**: 1-9 with confidence
- **Wing**: Secondary type influence
- **Instinctual Variant**: Self-preservation, Social, Sexual
- **Integration Direction**: Growth direction (type they move toward when healthy)
- **Disintegration Direction**: Stress direction (type they move toward under stress)

#### Introversion Level

- Separate metric from MBTI E/I dimension
- Measures social energy preference independent of personality type

### 3. **Mental Health Indicators** - Expanded from 7 to 13

**New indicators added:**

- Eating disorder indicators
- Dissociation indicators
- Body dysmorphia indicators
- Substance use risk
- Addiction vulnerability
- Autism spectrum indicators

**Existing indicators:**

- Depression indicators
- Anxiety indicators
- PTSD symptoms
- OCD tendencies
- Bipolar markers
- ADHD traits
- Psychotic symptoms risk

### 4. **Defense Mechanisms** - Categorized (NEW!)

Now organized into three maturity levels:

#### Mature/Adaptive (Healthy)

- Humor
- Sublimation
- Suppression
- Altruism
- Anticipation

#### Neurotic/Intermediate

- Intellectualization
- Rationalization
- Displacement
- Reaction formation
- Undoing

#### Immature/Maladaptive

- Denial
- Projection
- Splitting
- Acting out
- Passive aggression

**Plus:** Primary mechanisms (most frequently used overall)

### 5. **Blindspots** (NEW!)

Array of 3-5 potential self-perception gaps:

- Areas where self-perception may differ from others' perception
- Patterns the person may not recognize in themselves
- Unconscious biases or assumptions
- Strengths they undervalue
- Weaknesses they overlook

### 6. **Cognitive Distortions** - Expanded from 5 to 8

**New additions:**

- Mental filtering (focusing on negatives, ignoring positives)
- Mind reading (assuming you know what others think)
- Fortune telling (predicting negative outcomes)

**Existing:**

- Black and white thinking
- Catastrophizing
- Overgeneralization
- Personalization
- Should statements

### 7. **Psychological Traits** - Expanded from 6 to 8

**New additions:**

- Emotional stability (vs neuroticism)
- Open-mindedness (intellectual curiosity, openness to new ideas)

**Existing:**

- Impulsivity
- Perfectionism
- Self-esteem
- Resilience
- Optimism/Pessimism
- Assertiveness

### 8. **Attachment Style** - Enhanced

**New dimensions:**

- Disorganization level (fear + desire for closeness)
- Earned-secure type (secure despite difficult past)

**Existing dimensions:**

- Primary type (secure, anxious, avoidant, fearful-avoidant)
- Security score
- Anxiety dimension
- Avoidance dimension

### 9. **Confidence Scores** - On ALL Metrics! (NEW!)

**Every metric now includes:**

```json
{
  "value": 0.75,
  "confidence": 0.85
}
```

**Calculation:**

- Base confidence = min(0.95, message_count / 200.0)
- Per-metric adjustments based on:
  - Direct evidence in messages
  - Pattern consistency
  - Contradictory evidence presence
  - Sample diversity

**Sample Size Recommendations:**

- 200+ messages = 95% confidence baseline
- 100-200 messages = 70-95% confidence
- 50-100 messages = 50-70% confidence
- <50 messages = <50% confidence

### 10. **Behavioral Styles** - Now with Confidence

Converted from simple strings to confidence format:

- Learning style: visual/auditory/kinesthetic/reading-writing (with confidence %)
- Conflict resolution style: competing/collaborating/compromising/avoiding/accommodating (with confidence %)
- Decision making style: analytical/intuitive/collaborative/avoidant (with confidence %)

## Display Updates

The admin GUI now shows:

1. **Executive summary at the top** - gives you the "TL;DR" before diving into details
2. **Confidence percentages** next to every metric - e.g., `[████████░░] 0.75 (85% confident)`
3. **MBTI visualization** - shows all 4 dimensions + final type
4. **Enneagram details** - type, wing, growth/stress directions
5. **Categorized defense mechanisms** - separated by maturity level
6. **Blindspots section** - self-awareness gaps
7. **All new mental health indicators** - eating disorder, dissociation, etc.
8. **Sample size info** - tells you how many more messages needed for 95% confidence

## Profile Caching for Personalization

### **Question** "Would caching these results and allowing the bot/LLM use them be helpful or massive overkill?"

### **Answer** This is NOT overkill - it's the ESSENCE of personalized AI!

### Why It's Valuable

#### 1. **Language Adaptation**

- Adjust vocabulary complexity based on estimated IQ
- High IQ (>115): Use advanced vocabulary, complex sentence structures
- Average IQ (85-115): Use moderate complexity
- Lower IQ (<85): Use simple, clear language

#### 2. **Emotional Tone Matching**

- **Secure attachment**: Direct, balanced communication
- **Anxious attachment**: Warm, reassuring, emphasize consistency
- **Avoidant attachment**: Respect space, less emotionally intense, focus on independence
- **Disorganized**: Highly consistent and predictable responses

#### 3. **Cognitive Distortion Awareness**

- High catastrophizing: Avoid worst-case scenarios, focus on realistic outcomes
- Black-and-white thinking: Emphasize nuance, shades of gray
- Personalization: Remind that not everything is about them
- Fortune telling: Challenge negative predictions with evidence

#### 4. **Learning Style Optimization**

- **Visual learners**: Use analogies, metaphors, "picture this" language
- **Auditory learners**: Use rhythm, repetition, "listen to this" framing
- **Kinesthetic learners**: Use action-oriented language, physical metaphors
- **Reading-writing**: Provide structured lists, written summaries

#### 5. **Defense Mechanism Awareness**

- **Intellectualization**: Match their analytical approach, provide logical frameworks
- **Humor**: Mirror that lightness, use appropriate wit
- **Denial**: Gently introduce reality without confrontation
- **Sublimation**: Encourage channeling into productive outlets

#### 6. **Motivation Alignment**

- **High achievement**: Frame goals as accomplishments, emphasize success metrics
- **High autonomy**: Emphasize personal choice and control
- **High affiliation**: Frame benefits in terms of relationships and belonging
- **High power**: Frame as increasing influence and impact

#### 7. **MBTI-Based Communication**

- **INTJ/INTP**: Logic, systems, efficiency - minimize emotional fluff
- **ENFP/ENTP**: Possibilities, brainstorming, enthusiasm - embrace spontaneity
- **ISFJ/ESFJ**: Warmth, tradition, practical help - emphasize care and duty
- **ESTP/ESFP**: Action, excitement, real-world application - keep it dynamic

#### 8. **Enneagram Adaptation**

- **Type 1 (Perfectionist)**: Acknowledge high standards, help with self-compassion
- **Type 2 (Helper)**: Validate their giving nature, encourage self-care
- **Type 3 (Achiever)**: Celebrate accomplishments, help with authenticity
- **Type 4 (Individualist)**: Honor their uniqueness, help with stability
- **Type 5 (Investigator)**: Respect their need for knowledge and space
- **Type 6 (Loyalist)**: Provide security, address anxieties
- **Type 7 (Enthusiast)**: Match energy, help with focus
- **Type 8 (Challenger)**: Be direct, respect their strength
- **Type 9 (Peacemaker)**: Gentle encouragement, help with assertion

#### 9. **Blindspot Guidance**

- When they're missing something obvious, provide perspective
- Highlight strengths they undervalue
- Gently point out patterns they don't see
- Help them recognize contradictions

#### 10. **Mental Health Sensitivity**

- **High depression**: More encouraging language, celebrate small wins
- **High anxiety**: Provide structure, predictability, reassurance
- **ADHD traits**: Use short paragraphs, clear structure, summaries
- **OCD tendencies**: Acknowledge their concerns without reinforcing rituals
- **Eating disorder indicators**: Avoid weight/body talk, focus on health/wellbeing
- **Substance use risk**: Emphasize healthy coping alternatives

### Implementation Guide

Add to `handle_message()` method after `ensure_user()`:

```python
# Load cached psychological profile for personalization
with db_ro() as conn:
    profile = conn.execute(
        "SELECT profile_data FROM psychological_profiles WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    if profile:
        profile_data = json.loads(profile[0])

        # Extract key metrics using helper function
        def get_metric(data, key, default=0.0):
            if key not in data:
                return default, 0.0
            val = data[key]
            if isinstance(val, dict) and 'value' in val:
                return val['value'], val.get('confidence', 0.0)
            return val, 0.0

        # Build personalization context
        personalization = {}

        # IQ-based vocabulary
        if 'cognitive_metrics' in profile_data:
            iq, _ = get_metric(profile_data['cognitive_metrics'], 'estimated_iq', 100)
            personalization['vocab_level'] = 'advanced' if iq > 115 else 'moderate' if iq > 85 else 'simple'

        # Attachment-based tone
        if 'attachment_style' in profile_data:
            att_style = profile_data['attachment_style'].get('primary_type', 'secure')
            if att_style == 'anxious':
                personalization['tone'] = 'warm and reassuring'
            elif att_style == 'avoidant':
                personalization['tone'] = 'respectful of independence'
            elif att_style == 'disorganized':
                personalization['tone'] = 'highly consistent'
            else:
                personalization['tone'] = 'balanced'

        # MBTI-based style
        if 'personality_typing' in profile_data:
            mbti = profile_data['personality_typing'].get('myers_briggs', {}).get('type', '')
            personalization['mbti'] = mbti
            if 'INTJ' in mbti or 'INTP' in mbti:
                personalization['style'] = 'logical and systematic'
            elif 'ENFP' in mbti or 'ENTP' in mbti:
                personalization['style'] = 'enthusiastic and possibility-focused'
            # ... etc

        # Add to system prompt or use in response generation
        # This is your personalization secret sauce!
```

## Technical Details

### Database Schema

```sql
CREATE TABLE psychological_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    profile_data TEXT NOT NULL,  -- JSON with all metrics
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);
```

### JSON Structure Example

```json
{
  "executive_summary": {
    "overview": "...",
    "most_prominent_traits": ["...", "..."],
    "core_strengths": ["...", "..."],
    "core_weaknesses": ["...", "..."],
    "overall_functioning": "high/moderate/low",
    "therapeutic_recommendations": ["...", "..."],
    "messages_analyzed": 150,
    "estimated_messages_for_95_confidence": 50
  },
  "personality_typing": {
    "myers_briggs": {
      "type": "INTJ",
      "confidence": 0.85,
      "dimensions": {
        "introversion_extraversion": { "score": -0.6, "confidence": 0.9 },
        "sensing_intuition": { "score": 0.7, "confidence": 0.85 },
        "thinking_feeling": { "score": -0.5, "confidence": 0.8 },
        "judging_perceiving": { "score": -0.4, "confidence": 0.75 }
      }
    },
    "enneagram": {
      "primary_type": 5,
      "wing": "w4",
      "confidence": 0.8,
      "instinctual_variant": "sp/sx",
      "integration_direction": 8,
      "disintegration_direction": 7
    },
    "introversion_level": { "value": 0.75, "confidence": 0.9 }
  },
  "mental_health_indicators": {
    "depression_indicators": { "value": 0.3, "confidence": 0.85 },
    "anxiety_indicators": { "value": 0.6, "confidence": 0.9 },
    "eating_disorder_indicators": { "value": 0.1, "confidence": 0.7 },
    "dissociation_indicators": { "value": 0.2, "confidence": 0.65 },
    "body_dysmorphia_indicators": { "value": 0.15, "confidence": 0.6 },
    "substance_use_risk": { "value": 0.05, "confidence": 0.75 },
    "addiction_vulnerability": { "value": 0.25, "confidence": 0.7 },
    "autism_spectrum_indicators": { "value": 0.4, "confidence": 0.65 }
    // ... etc for all 13 indicators
  },
  "defense_mechanisms": {
    "mature_adaptive": ["humor", "sublimation", "suppression"],
    "neurotic_intermediate": ["intellectualization", "rationalization"],
    "immature_maladaptive": ["denial", "projection"],
    "primary_mechanisms": ["intellectualization", "humor"]
  },
  "blindspots": [
    "May underestimate emotional impact on others",
    "Difficulty recognizing when logic won't solve the problem",
    "Tendency to overlook practical details in favor of theory"
  ],
  "cognitive_distortions": {
    "black_and_white_thinking": { "value": 0.4, "confidence": 0.8 },
    "catastrophizing": { "value": 0.5, "confidence": 0.85 },
    "mental_filtering": { "value": 0.6, "confidence": 0.75 },
    "mind_reading": { "value": 0.3, "confidence": 0.7 },
    "fortune_telling": { "value": 0.45, "confidence": 0.8 }
    // ... etc for all 8
  }
  // ... all other categories with confidence scores
}
```

## Testing the New Profile

1. **Generate a new profile** for a user with 100+ messages
2. **Check the Admin GUI** - open Psych Profile tab, select user, click "Analyze"
3. **Verify executive summary** appears at top
4. **Verify confidence scores** show next to all metrics
5. **Verify MBTI type** is displayed with all 4 dimensions
6. **Verify Enneagram** shows type + wing
7. **Verify blindspots** section appears
8. **Verify defense mechanisms** are categorized (mature/neurotic/immature)
9. **Check sample size message** - should tell you how many more messages needed

## Future Enhancements Roadmap

### Possible additions (marked as #TODO in code):

- **Jungian shadow work**: Identify repressed aspects
- **Narrative identity**: Extract life story themes
- **Values hierarchy**: Rank personal values
- **Relationship patterns**: Analyze interpersonal dynamics across different relationship types
- **Stress response profile**: How they react under different stressors
- **Emotional regulation strategies**: Preferred methods and effectiveness
- **Cognitive biases**: Beyond distortions - confirmation bias, availability heuristic, etc.
- **Moral development stage**: Kohlberg's stages
- **Existential concerns**: Death anxiety, meaninglessness, isolation, freedom
- **Somatic markers**: Physical sensations associated with emotions

## Performance Considerations

- **Analysis time**: ~30-60 seconds for 100+ messages (depends on model speed)
- **Storage**: ~5-10KB per profile (JSON compressed)
- **Memory**: Negligible - profiles loaded on-demand
- **Caching**: Recommended - load once per session, reuse across messages
- **Update frequency**: Run analysis weekly or after significant message count increase (50+ new messages)

## Confidence Optimization

To improve confidence scores:

- **More messages**: 200+ messages gives 95% baseline
- **Diverse topics**: Coverage across life domains (work, relationships, hobbies, struggles)
- **Emotional range**: Messages showing different moods and states
- **Time span**: Messages across weeks/months show patterns vs. one-off events
- **Depth**: Longer, more reflective messages provide richer data than short responses

## Ethical Considerations

⚠️ **Important Reminders:**

- This is **not a diagnostic tool** - it's for personalization only
- Confidence scores reflect statistical certainty, not clinical validity
- High mental health indicator scores warrant human review
- Users should be informed that analysis is happening (privacy/consent)
- Profiles should be encrypted at rest if containing sensitive data
- Regular audits for bias in LLM-generated assessments
- Clear communication that this is AI-based pattern recognition, not professional assessment

---

**Last Updated:** 2025-01-XX
**Version:** 2.0 - Exhaustive Expansion
**Author:** Wellness Bot Development Team
