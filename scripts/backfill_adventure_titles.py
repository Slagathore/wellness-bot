"""Backfill descriptive titles for adventures stuck on placeholder names.

The in-app auto-titler only fires during active play, and its placeholder regex
misses the default `Adventure <timestamp>` format (and single-word botched
titles), so older/dormant adventures keep generic names forever. This scans for
those, generates a title from the opening messages using the local chat model
(abliterated, so NSFW roleplay titles fine), and rewrites them.

    python -m scripts.backfill_adventure_titles            # dry run — shows proposals
    python -m scripts.backfill_adventure_titles --apply    # write them

Safe to run while the bot is up (WAL + busy_timeout). --apply prints an old->new
map you can eyeball, and only the `title` column is touched.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3

import httpx

from app.config import settings
from app.interfaces.telegram.adapter import TelegramAdapter

# Generic == the default `Adventure <timestamp>` / `Adventure N`, known
# placeholders, empty, or a botched auto-title (a single word or ~2 chars).
_GENERIC_RE = re.compile(
    r"^(adventure(\s|$)|untitled\b|converted adventure$|new adventure$|quick adventure$)",
    re.IGNORECASE,
)


def is_generic(title: str | None) -> bool:
    text = (title or "").strip()
    if not text:
        return True
    if _GENERIC_RE.match(text):
        return True
    return len(text) <= 2 or len(text.split()) < 2  # "A", "Digital", etc.


def generate_title(exchange_text: str, host: str, model: str) -> str | None:
    body = {
        "model": model,
        "stream": False,
        "options": {"num_predict": 600, "temperature": 0.35},
        "messages": [
            {"role": "system", "content": (
                "You are a creative writing assistant. Based on the roleplay excerpt "
                "below, produce a short evocative title (3 to 6 words). Reply with ONLY "
                "the title — no quotes, no trailing punctuation, no explanation, no reasoning."
            )},
            {"role": "user", "content": f"Roleplay excerpt:\n{exchange_text}\n\nTitle:"},
        ],
    }
    resp = httpx.post(f"{host}/api/chat", json=body, timeout=180.0)
    resp.raise_for_status()
    raw = (resp.json().get("message") or {}).get("content", "")
    return TelegramAdapter._clean_generated_title(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the renames (default: dry run)")
    ap.add_argument("--db", help="override DB path (default: settings/DATABASE_PATH)")
    ap.add_argument("--model", help="override chat model")
    args = ap.parse_args()

    cfg = settings()
    host = cfg.ollama_host.rstrip("/")
    model = args.model or cfg.chat_model
    db_path = args.db or os.environ.get("DATABASE_PATH") or cfg.database_path
    print(f"DB: {db_path}\nOllama: {host}  model: {model}\n")

    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=8000")
    rows = con.execute("SELECT id, title FROM adventures ORDER BY id").fetchall()

    changes: list[tuple[int, str, str]] = []
    skipped_empty: list[int] = []
    for r in rows:
        if not is_generic(r["title"]):
            continue
        msgs = con.execute(
            "SELECT role, content FROM adventure_messages "
            "WHERE adventure_id = ? AND role IN ('user', 'narrator') ORDER BY id ASC LIMIT 10",
            (r["id"],),
        ).fetchall()
        if not msgs:
            skipped_empty.append(r["id"])
            print(f"  #{r['id']:>3}  SKIP (no messages)          {r['title']!r}")
            continue
        exchange = "\n".join(
            f"{'Player' if m['role'] == 'user' else 'Narrator'}: {m['content'][:200]}"
            for m in msgs
        )
        title = None
        for _ in range(2):
            try:
                title = generate_title(exchange, host, model)
            except Exception as exc:  # noqa: BLE001
                print(f"  #{r['id']:>3}  ERROR: {exc}")
                break
            if title:
                break
        if title:
            changes.append((r["id"], r["title"] or "", title))
            print(f"  #{r['id']:>3}  {(r['title'] or '')!r:32} -> {title!r}")
        else:
            print(f"  #{r['id']:>3}  could not generate (kept)   {r['title']!r}")

    print()
    if not changes:
        print("Nothing to rename.")
    elif args.apply:
        for aid, _old, new in changes:
            con.execute(
                "UPDATE adventures SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new, aid),
            )
        con.commit()
        print(f"Applied {len(changes)} renames.")
    else:
        print(f"Dry run — {len(changes)} would be renamed. Re-run with --apply.")
    if skipped_empty:
        print(f"Skipped {len(skipped_empty)} empty adventure(s) with no content to summarize: "
              f"{skipped_empty}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
