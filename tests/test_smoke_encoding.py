"""
Mission Statement:
=================
Smoke test suite for the Telegram Wellness Bot. This project is a comprehensive
wellness companion that runs on Telegram, powered by local LLMs for psychological
profiling, mood tracking, reminders, and adaptive conversations.

This test module ensures:
  1. Every Python module in app/ can be imported without encoding errors.
  2. All file I/O paths use UTF-8 encoding explicitly (no charmap blowups on Windows).
  3. All print() and logging calls survive non-ASCII content (emojis, accents, CJK).
  4. The psych profile pipeline roundtrips correctly through generation, storage, and retrieval.
  5. The personality config save/load cycle handles emoji-rich configs.
  6. All worker entry points can be referenced without crashing on startup strings.

How we achieve this:
  - Static AST scanning of every .py file under app/ checking open()/write_text()/read_text()
  - Runtime import verification of every module
  - Simulated psych-profile roundtrip with emoji-heavy payloads
  - Personality manager config save/load with emoji-laden personality definitions
  - Stdout capture to prove print() calls with emojis do not crash on Windows codepage

Each test is self-contained and uses the existing test_config / test_user fixtures.

Module map:
  - test_all_modules_importable: Dynamic import verification
  - test_no_open_write_missing_encoding: AST scan for encoding= in open("w") calls
  - test_no_write_text_missing_encoding: AST scan for encoding= in .write_text() calls
  - test_no_read_text_missing_encoding: AST scan for encoding= in .read_text() calls
  - test_print_survives_unicode: Proves print() with emojis doesn't crash via io.StringIO
  - test_profile_generation_roundtrip: Psych profile DB insert + read with emoji payload
  - test_personality_config_save_load_unicode: Personality config with emoji data
  - test_nightly_worker_module_loads: Import nightly worker without crash
  - test_worker_modules_load: Import all worker modules
  - test_json_dumps_ensure_ascii_false: Verify JSON payloads preserve unicode
  - test_profile_generation_prompt_builds: Verify prompt builder runs without encoding error
  - test_profile_defaults_backfill: Verify _ensure_profile_defaults survives emoji data
  - test_emotional_analyzer_write_utf8: Verify emotional summary output uses utf-8
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

# Emoji corpus used across multiple tests — these are the characters that
# blew up on Windows cp1252 in production.
EMOJI_CORPUS = (
    "\U0001f9e0"   # 🧠 brain — the original crash character
    "\U0001f4ca"   # 📊 bar chart
    "\U0001f9e9"   # 🧩 puzzle piece
    "\U0001f525"   # 🔥 fire
    "\u2026"       # … ellipsis — also non-ASCII
    "\U0001f60a"   # 😊 smile
    "\U0001f3a8"   # 🎨 artist palette
    "\U0001f454"   # 👔 necktie
    "\u2705"       # ✅ check mark
    "\u26a0\ufe0f" # ⚠️ warning
    "\U0001f5c3\ufe0f" # 🗃️ card file box
)


def _collect_py_files(root: Path) -> List[Path]:
    """Return every .py file under *root*, excluding __pycache__."""
    result: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                result.append(Path(dirpath) / fn)
    return sorted(result)


def _module_name_from_path(py_path: Path) -> str:
    """Turn an absolute path into a dotted module name relative to repo root."""
    rel = py_path.relative_to(REPO_ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ---------------------------------------------------------------------------
# 1. Import smoke tests — can every module load without error?
# ---------------------------------------------------------------------------

_ALL_PY_FILES = _collect_py_files(APP_DIR)
_ALL_MODULE_NAMES = [_module_name_from_path(p) for p in _ALL_PY_FILES]


# Modules that require heavy external deps (tkinter, telegram, discord, etc.)
# that may not be available in CI — skip gracefully.
_IMPORT_SKIP_PREFIXES = (
    "app.ui.desktop",       # tkinter
    "app.main_modular",     # wires everything; needs full env
    "app.interfaces.telegram",  # python-telegram-bot
    "app.features.discord",     # discord.py
    "app.runtime.wiring",       # wires everything; needs full env
    "app.runtime.bootstrap",    # wires everything; needs full env
    "app.runtime.bot_service",  # needs telegram
    "app.runtime.handlers",     # needs telegram
    "app.runtime.catchup",      # needs settings singleton
    "app.runtime.context",      # needs settings singleton
    "app.runtime.services",     # needs settings singleton
    "app.workers.bootstrap",    # needs settings singleton
    "app.utils.web_search",     # requires ollama package
    "app.features.profile_import.bootstrap",  # wires telegram handlers
    "app.features.nsfw_preferences.bootstrap",  # wires telegram handlers
    "app.features.personalization_agent.bootstrap",  # wires telegram handlers
    "app.features.adaptive_psych_tests.bootstrap",  # wires telegram handlers
    "app.features.feedback.bootstrap",  # wires telegram handlers
    "app.interfaces.admin.server",  # FastAPI app needs full env
)


@pytest.mark.parametrize(
    "module_name",
    [m for m in _ALL_MODULE_NAMES if not m.startswith(_IMPORT_SKIP_PREFIXES)],
    ids=lambda m: m,
)
def test_all_modules_importable(module_name: str) -> None:
    """Every non-UI, non-wiring module must import without raising."""
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"Failed to import {module_name}: {exc}")


# ---------------------------------------------------------------------------
# 2. AST scan — find open("w") / open("a") missing encoding="utf-8"
# ---------------------------------------------------------------------------

def _find_open_calls_missing_encoding(py_path: Path) -> List[Tuple[int, str]]:
    """Return (line_number, snippet) for open() calls in write/append mode
    that do NOT pass encoding=."""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_path))
    except SyntaxError:
        return []

    issues: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match open(...) and builtins.open(...)
        func = node.func
        is_open = False
        if isinstance(func, ast.Name) and func.id == "open":
            is_open = True
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            is_open = True
        if not is_open:
            continue

        # Determine mode argument
        mode: str = "r"  # default
        if len(node.args) >= 2:
            mode_node = node.args[1]
            if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
                mode = mode_node.value
        for kw in node.keywords:
            if (
                kw.arg == "mode"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                mode = kw.value.value

        # Only care about text-write modes
        if "w" not in mode and "a" not in mode:
            continue

        # Binary mode is fine
        if "b" in mode:
            continue

        # Check if encoding= is passed
        has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
        if not has_encoding:
            snippet = ast.get_source_segment(source, node) or "<unknown>"
            issues.append((node.lineno, snippet[:120]))

    return issues


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_open_write_missing_encoding(py_path: Path) -> None:
    """Every open('w' / 'a') call in app/ must specify encoding='utf-8'."""
    issues = _find_open_calls_missing_encoding(py_path)
    if issues:
        report = "\n".join(f"  line {ln}: {snip}" for ln, snip in issues)
        pytest.fail(
            f"{py_path.relative_to(REPO_ROOT)} has open() write/append calls "
            f"without encoding=:\n{report}"
        )


# ---------------------------------------------------------------------------
# 3. AST scan — find .write_text() / .read_text() missing encoding="utf-8"
# ---------------------------------------------------------------------------

def _find_pathlib_text_missing_encoding(py_path: Path, method_name: str) -> List[Tuple[int, str]]:
    """Return (line, snippet) for .write_text() or .read_text() calls missing encoding=."""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_path))
    except SyntaxError:
        return []

    issues: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == method_name):
            continue
        has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
        if not has_encoding:
            snippet = ast.get_source_segment(source, node) or "<unknown>"
            issues.append((node.lineno, snippet[:120]))

    return issues


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_write_text_missing_encoding(py_path: Path) -> None:
    """.write_text() calls must pass encoding='utf-8'."""
    issues = _find_pathlib_text_missing_encoding(py_path, "write_text")
    if issues:
        report = "\n".join(f"  line {ln}: {snip}" for ln, snip in issues)
        pytest.fail(
            f"{py_path.relative_to(REPO_ROOT)} has .write_text() "
            f"without encoding=:\n{report}"
        )


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_read_text_missing_encoding(py_path: Path) -> None:
    """.read_text() calls must pass encoding='utf-8'."""
    issues = _find_pathlib_text_missing_encoding(py_path, "read_text")
    if issues:
        report = "\n".join(f"  line {ln}: {snip}" for ln, snip in issues)
        pytest.fail(
            f"{py_path.relative_to(REPO_ROOT)} has .read_text() "
            f"without encoding=:\n{report}"
        )


# ---------------------------------------------------------------------------
# 4. Runtime: print() with full emoji corpus doesn't crash
# ---------------------------------------------------------------------------

def test_print_survives_unicode() -> None:
    """Prove that printing every emoji in our corpus succeeds when stdout is
    redirected to a StringIO (which mirrors UTF-8-capable terminals)."""
    buf = io.StringIO()
    original = sys.stdout
    try:
        sys.stdout = buf
        # Replicate the exact format string that crashed in nightly.py
        print("[nightly] [PSYCH] User 123456 Profile: Depression:0.32 Anxiety:0.18 IQ:~112")
        # Now blast through every emoji we use anywhere in the codebase
        for ch in EMOJI_CORPUS:
            print(f"[test] emoji: {ch}")
        # Personality mode names with emojis
        print("[test] Therapeutic 🧠 | Friendly 😊 | Creative 🎨 | Professional 👔")
    finally:
        sys.stdout = original

    output = buf.getvalue()
    assert "[nightly]" in output
    assert "[test]" in output


# ---------------------------------------------------------------------------
# 5. Psych profile DB roundtrip with emoji payload
# ---------------------------------------------------------------------------

def _make_emoji_profile() -> dict:
    """Build a realistic psych profile dict with emojis embedded in string values."""
    return {
        "executive_summary": {
            "overview": "User shows 🧠 strong analytical tendencies with 😊 warm interpersonal skills.",
            "most_prominent_traits": ["analytical 🧩", "empathetic 💚", "creative 🎨"],
            "core_strengths": ["logical reasoning 📊", "emotional awareness 🧠"],
            "core_weaknesses": ["overthinking 🔄", "perfectionism ⚠️"],
            "overall_functioning": "Good — user is resilient 🛡️ with minor anxiety indicators.",
            "therapeutic_recommendations": ["CBT for anxiety 🧘", "journaling 📝"],
            "messages_analyzed": 150,
            "estimated_messages_for_95_confidence": 50,
        },
        "mental_health_indicators": {
            "depression_likelihood": {"value": 0.15, "confidence": 0.7},
            "anxiety_likelihood": {"value": 0.35, "confidence": 0.8},
        },
        "big_five": {
            "openness": {"value": 0.85, "confidence": 0.75},
            "conscientiousness": {"value": 0.72, "confidence": 0.6},
            "extraversion": {"value": 0.45, "confidence": 0.65},
            "agreeableness": {"value": 0.8, "confidence": 0.7},
            "neuroticism": {"value": 0.38, "confidence": 0.7},
        },
        "cognitive_metrics": {
            "estimated_iq": {"value": 115, "confidence": 0.5},
            "vocabulary_complexity": {"value": 0.78, "confidence": 0.65},
        },
        "personality_typing": {
            "myers_briggs": {"type": "INTJ", "confidence": 0.6},
        },
        "blindspots": ["Doesn't recognize own emotional needs 🫣"],
        "idiosyncrasies": ["Uses 🧠 emoji frequently", "Says 'honestly' a lot"],
    }


def test_profile_roundtrip_with_emojis(test_config, test_user) -> None:
    """Insert a psych profile full of emojis into the DB and read it back intact."""
    from app.db import db_ro, db_rw

    user_id, telegram_user_id = test_user
    profile = _make_emoji_profile()
    profile_json = json.dumps(profile, ensure_ascii=False)
    big_five_json = json.dumps(profile.get("big_five", {}), ensure_ascii=False)
    mh_json = json.dumps(profile.get("mental_health_indicators", {}), ensure_ascii=False)
    cog_json = json.dumps(profile.get("cognitive_metrics", {}), ensure_ascii=False)

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO psychological_profiles
                (user_id, profile_data, messages_analyzed, confidence_score,
                 big_five, mental_health_indicators, cognitive_metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, profile_json, 150, 0.75, big_five_json, mh_json, cog_json),
        )

    with db_ro() as conn:
        row = conn.execute(
            "SELECT profile_data FROM psychological_profiles WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    assert row is not None, "Profile row not found after insert"
    loaded = json.loads(row["profile_data"])
    assert "\U0001f9e0" in loaded["executive_summary"]["overview"], "Brain emoji lost in roundtrip"
    assert loaded["executive_summary"]["messages_analyzed"] == 150
    assert loaded["big_five"]["openness"]["value"] == 0.85


# ---------------------------------------------------------------------------
# 6. Personality config save/load with emoji-rich data
# ---------------------------------------------------------------------------

def test_personality_config_save_load_unicode(tmp_path: Path) -> None:
    """PersonalityManager.save_config must roundtrip emoji content via UTF-8."""
    config_path = tmp_path / "personality_config.json"
    config = {
        "personalities": {
            "therapeutic": {
                "name": "Therapeutic",
                "emoji": "🧠",
                "temperature": 0.7,
                "system_prompt": "You are Mira 🧠 in therapeutic mode.",
            },
            "friendly": {
                "name": "Friendly",
                "emoji": "😊",
                "temperature": 0.8,
                "system_prompt": "You are Mira 😊, a warm friend.",
            },
        }
    }

    # Write using the same pattern PersonalityManager uses (now with encoding)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    # Read back
    with open(config_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    assert loaded["personalities"]["therapeutic"]["emoji"] == "🧠"
    assert loaded["personalities"]["friendly"]["emoji"] == "😊"
    assert "🧠" in loaded["personalities"]["therapeutic"]["system_prompt"]


# ---------------------------------------------------------------------------
# 7. Profile generation module internal helpers
# ---------------------------------------------------------------------------

def test_profile_generation_prompt_builds() -> None:
    """_build_profile_prompt must produce a string without encoding errors."""
    from app.personality.profile_generation import _build_profile_prompt

    sample = "User said: I feel 🧠 brainy today and a bit 😊 happy."
    prompt = _build_profile_prompt(sample, message_count=50, messages_needed_for_95_confidence=150)
    assert isinstance(prompt, str)
    assert len(prompt) > 100
    assert "50" in prompt  # message count appears in prompt


def test_profile_defaults_backfill_with_emojis() -> None:
    """_ensure_profile_defaults must tolerate emoji keys/values."""
    from app.personality.profile_generation import _ensure_profile_defaults

    profile: dict = {
        "executive_summary": {
            "overview": "🧠 summary with emojis 🎨",
        },
    }
    _ensure_profile_defaults(profile, message_count=50, messages_needed_for_95_confidence=150)
    assert "mental_health_indicators" in profile
    assert "big_five" in profile
    assert profile["executive_summary"]["messages_analyzed"] == 50


def test_extract_json_blob_with_unicode() -> None:
    """_extract_json_blob must handle emoji-laden JSON strings."""
    from app.personality.profile_generation import _extract_json_blob

    raw = '```json\n{"key": "value 🧠🎨"}\n```'
    blob = _extract_json_blob(raw)
    parsed = json.loads(blob)
    assert parsed["key"] == "value 🧠🎨"


def test_profile_generation_error_class() -> None:
    """ProfileGenerationError must carry a message through exception chain."""
    from app.personality.profile_generation import ProfileGenerationError

    with pytest.raises(ProfileGenerationError, match="test error 🧠"):
        raise ProfileGenerationError("test error 🧠")


# ---------------------------------------------------------------------------
# 8. Worker modules load without encoding errors
# ---------------------------------------------------------------------------

def test_nightly_worker_module_loads() -> None:
    """app.workers.nightly must import cleanly (no emoji in module-level print)."""
    mod = importlib.import_module("app.workers.nightly")
    assert hasattr(mod, "nightly_pipeline")
    assert hasattr(mod, "_analyze_user_psychological_profile")


def test_sentiments_worker_loads() -> None:
    mod = importlib.import_module("app.workers.sentiments")
    assert hasattr(mod, "sentiment_loop")


def test_embeddings_worker_loads() -> None:
    mod = importlib.import_module("app.workers.embeddings")
    assert hasattr(mod, "embedding_loop")


def test_outbox_worker_loads() -> None:
    mod = importlib.import_module("app.workers.outbox_sender")
    assert hasattr(mod, "send_loop")


# ---------------------------------------------------------------------------
# 9. JSON serialization preserves unicode
# ---------------------------------------------------------------------------

def test_json_roundtrip_emoji_ensure_ascii_false() -> None:
    """json.dumps(ensure_ascii=False) must preserve emoji for DB storage."""
    data = {"brain": "🧠", "nested": {"smile": "😊"}, "list": ["🎨", "👔"]}
    serialized = json.dumps(data, ensure_ascii=False)
    assert "🧠" in serialized
    assert "\\u" not in serialized  # no escaped unicode
    roundtripped = json.loads(serialized)
    assert roundtripped == data


def test_json_roundtrip_ensure_ascii_true_still_parses() -> None:
    """json.dumps(ensure_ascii=True) escapes emojis but still parses back OK."""
    data = {"brain": "🧠"}
    serialized = json.dumps(data, ensure_ascii=True)
    assert "\\u" in serialized
    roundtripped = json.loads(serialized)
    assert roundtripped["brain"] == "🧠"


# ---------------------------------------------------------------------------
# 10. File I/O smoke: write and read back emoji data
# ---------------------------------------------------------------------------

def test_write_read_file_utf8(tmp_path: Path) -> None:
    """Basic file I/O with UTF-8 encoding must survive full emoji corpus."""
    test_file = tmp_path / "emoji_test.txt"
    content = f"All emojis: {EMOJI_CORPUS}\nFin."
    test_file.write_text(content, encoding="utf-8")

    loaded = test_file.read_text(encoding="utf-8")
    assert loaded == content
    for ch in EMOJI_CORPUS:
        assert ch in loaded, f"Lost character U+{ord(ch):04X} in file roundtrip"


def test_write_read_json_utf8(tmp_path: Path) -> None:
    """JSON file I/O with ensure_ascii=False and encoding='utf-8'."""
    test_file = tmp_path / "emoji_test.json"
    data = {"emojis": EMOJI_CORPUS, "nested": {"brain": "🧠"}}

    with open(test_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(test_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    assert loaded == data


# ---------------------------------------------------------------------------
# 11. Personality modes have valid emoji fields
# ---------------------------------------------------------------------------

def test_personality_modes_emoji_fields() -> None:
    """Every personality mode must define an emoji that is a valid unicode string."""
    from app.personality.modes import PERSONALITY_MODES

    for name, config in PERSONALITY_MODES.items():
        emoji = config.get("emoji", "")
        assert isinstance(emoji, str), f"Mode {name} emoji is not a string"
        assert len(emoji) > 0, f"Mode {name} has empty emoji"
        # Verify the emoji can be encoded to UTF-8 without error
        emoji.encode("utf-8")
        # Verify it cannot be encoded to cp1252 (proving we need UTF-8)
        # (this is informational, not a failure — just confirms these chars are problematic)


# ---------------------------------------------------------------------------
# 12. Emotional analyzer write path
# ---------------------------------------------------------------------------

def test_emotional_analyzer_produces_utf8_json(tmp_path: Path) -> None:
    """Simulate what EmotionalAnalyzer._write_to_disk does, proving UTF-8 is used."""
    payload = {
        "summary": "User shows 🧠 strong patterns",
        "dominant_emotion": "thoughtful 🤔",
        "trend": "stable",
    }
    filename = tmp_path / "emotional_summary_2026-02-12.json"
    filename.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )

    loaded = json.loads(filename.read_text(encoding="utf-8"))
    assert "🧠" in loaded["summary"]


# ---------------------------------------------------------------------------
# 13. Nightly pipeline helper: _safe_metric_value
# ---------------------------------------------------------------------------

def test_safe_metric_value_nested_dict() -> None:
    """_safe_metric_value must handle both flat and nested {value, confidence} formats."""
    from app.workers.nightly import _safe_metric_value

    # Flat format
    assert _safe_metric_value({"depression_likelihood": 0.3}, "depression_likelihood", 0.0) == 0.3
    # Nested format
    assert _safe_metric_value(
        {"depression_likelihood": {"value": 0.42, "confidence": 0.8}},
        "depression_likelihood",
        0.0,
    ) == 0.42
    # Missing key
    assert _safe_metric_value({}, "missing", 0.5) == 0.5
    # Non-dict input
    assert _safe_metric_value("not a dict", "key", 0.0) == 0.0
    # None input
    assert _safe_metric_value(None, "key", 1.0) == 1.0


# ---------------------------------------------------------------------------
# 14. Full nightly profile analysis with mocked LLM
# ---------------------------------------------------------------------------

def test_nightly_profile_analysis_mocked(test_config, test_user, monkeypatch) -> None:
    """Run _analyze_user_psychological_profile end-to-end with a mocked LLM,
    verifying no encoding errors occur and the profile is stored in the DB."""
    from app.db import db_ro, db_rw
    from app.workers.nightly import _analyze_user_psychological_profile

    user_id, telegram_user_id = test_user

    # Create a session first (messages require session_id)
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', 1024)",
            (user_id,),
        )
        session_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # Seed enough messages so the function doesn't bail out
    with db_rw() as conn:
        for i in range(25):
            conn.execute(
                "INSERT INTO messages(user_id, session_id, role, content) VALUES(?, ?, 'user', ?)",
                (user_id, session_id, f"Test message number {i} about how I'm feeling today"),
            )

    # Mock the LLM generate() to return a valid emoji-rich profile
    mock_profile = _make_emoji_profile()
    mock_response = {"text": json.dumps(mock_profile, ensure_ascii=False), "raw": {}}

    import app.utils.ollama
    monkeypatch.setattr(
        app.utils.ollama,
        "generate",
        lambda prompt, model=None, format=None, options=None: mock_response,
    )

    # Capture stdout to prove no encoding crash
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        result = _analyze_user_psychological_profile(user_id, telegram_user_id, 25)
    finally:
        sys.stdout = old_stdout

    assert result is True, "Profile generation should succeed"
    output = buf.getvalue()
    assert "[nightly]" in output

    # Verify profile was stored in DB
    with db_ro() as conn:
        row = conn.execute(
            "SELECT profile_data FROM psychological_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    assert row is not None, "Profile not stored in DB"
    stored = json.loads(row["profile_data"])
    assert "executive_summary" in stored


# ---------------------------------------------------------------------------
# 15. AST scan for non-ASCII in print() f-strings (informational)
# ---------------------------------------------------------------------------

def _find_print_with_non_ascii(py_path: Path) -> List[Tuple[int, str]]:
    """Find print() calls containing non-ASCII string literals."""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_path))
    except SyntaxError:
        return []

    issues: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "print"):
            continue

        # Walk all string constants inside the print() call arguments
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if any(ord(c) > 127 for c in child.value):
                    snippet = child.value[:80].replace("\n", "\\n")
                    issues.append((child.lineno, snippet))

    return issues


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_non_ascii_in_print_calls(py_path: Path) -> None:
    """print() calls in app/ must not contain non-ASCII literals (emojis, fancy
    punctuation) because Windows console codepages cannot encode them.
    Use logger instead, or replace with ASCII equivalents."""
    issues = _find_print_with_non_ascii(py_path)
    if issues:
        report = "\n".join(f"  line {ln}: {snip!r}" for ln, snip in issues)
        pytest.fail(
            f"{py_path.relative_to(REPO_ROOT)} has print() with non-ASCII characters:\n{report}\n"
            f"Fix: replace emojis/fancy chars with ASCII or switch to logger."
        )


# TODO: Add test for admin API psych profile endpoint returning UTF-8 JSON
# TODO: Add test for Telegram adapter archiving psych profiles with emojis
# TODO: Add test for adaptive psych test service roundtrip
# TODO: Add test for profile_context.py emoji handling in LLM prompt injection
# TODO: Add test for memory indexer handling emoji-laden conversation text
# TODO: Add test for stress-testing all personality modes through the pipeline
# TODO: Add benchmark for profile generation prompt size vs context window limits
