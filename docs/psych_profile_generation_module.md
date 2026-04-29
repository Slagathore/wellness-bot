# Module Reference: `app/personality/profile_generation.py`

## Mission Statement

This module converts recent conversation transcripts into a structured psychological profile that the wellness bot can trust. It centralises the prompt template, the model invocation, and post-processing so that both the admin UI and nightly workers consume an identical, safety-checked payload. The goal is to keep personalization consistent, auditable, and reusable across the entire project.

## Modules and Classes Referenced

- `app.utils.ollama.generate` – Executes the synchronous LLM call that powers the analysis.
- `json` – Parses the returned JSON payload and prepares fallbacks.
- `logging` – Records failures and debugging breadcrumbs during generation.
- `dataclasses.dataclass` – Implements the `ProfileGenerationResult` value container.

## Constants

- `MAX_CONTEXT_CHARS` (70_000): Caps the amount of transcript text fed to the LLM to stay inside model context limits.
- `DEFAULT_TEMPERATURE` (0.15): Default decoding temperature chosen for deterministic clinical output.
- `MIN_MESSAGES_FOR_ANALYSIS` (20): Guard-rail ensuring profiles are not generated from statistically insignificant samples.

## Public API

### `ProfileGenerationError`

Raised whenever the module cannot produce valid structured data—covers upstream API failures, empty responses, and JSON parsing issues.

### `ProfileGenerationResult`

A frozen dataclass returning:

- `profile`: Parsed profile dictionary ready for persistence.
- `raw_text`: Raw LLM output retained for auditing.
- `message_count`: Number of user messages that fed the analysis.
- `messages_needed_for_95_confidence`: Remaining sample size suggested for statistical confidence.

### `generate_comprehensive_profile(conversation_sample, message_count, model=None, temperature=DEFAULT_TEMPERATURE)`

The main entry point shared by the GUI tooling and nightly worker. It trims the sample, assembles the exhaustive prompt (including therapeutic, partner, career, and insight sections), calls the LLM, validates the response, and back-fills required defaults before returning a `ProfileGenerationResult`.

## Internal Helpers

- `_build_profile_prompt(conversation_sample, message_count, messages_needed_for_95_confidence)`: Generates the full JSON schema prompt with explicit instructions for every metric.
- `_extract_json_blob(raw_text)`: Pulls the JSON object from raw model output, tolerating Markdown code fences.
- `_ensure_profile_defaults(profile, message_count, messages_needed_for_95_confidence)`: Injects required keys, list scaffolding, and summary metadata so downstream renderers never break on missing fields.

## Notable Variables and Structures

- `messages_needed_for_95_confidence`: Derived value indicating how many additional messages are required for a 95% confidence baseline; reported back to the admin UI.

## Future Enhancements

- Track justification snippets alongside each metric for deeper auditing (#TODO embedded in module).
- Introduce per-personality weighting so the generator highlights context-specific nuances (#TODO embedded in module).
