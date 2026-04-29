from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app.domain.turns.llm_analyzer import LLMTurnAnalyzer
from app.domain.turns.planner import TurnPlanner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run heuristic vs LLM turn-planner shadow comparisons."
    )
    parser.add_argument(
        "--message",
        action="append",
        required=True,
        help="Message text to analyze. Pass multiple times for multiple cases.",
    )
    parser.add_argument("--user-id", type=int, default=1, help="User ID to analyze as.")
    parser.add_argument("--session-id", type=int, default=None, help="Optional session ID.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    planner = TurnPlanner(
        analyzer=LLMTurnAnalyzer(),
        shadow_enabled=True,
        llm_primary_enabled=False,
    )

    for index, message in enumerate(args.message, start=1):
        plan = planner.build_plan(
            user_id=args.user_id,
            session_id=args.session_id,
            message_text=message,
        )
        shadow = plan.shadow_comparison or {}
        print(f"\n=== Shadow Smoke {index} ===")
        print(f"Message: {message}")
        print(f"Planner source: {plan.planner_source}")
        print(f"Heuristic latency (ms): {shadow.get('heuristic_latency_ms', 'n/a')}")
        print(f"LLM latency (ms): {shadow.get('llm_latency_ms', 'n/a')}")
        print(f"Mismatches: {', '.join(shadow.get('mismatch_fields', [])) or 'none'}")
        print("Heuristic summary:")
        print(json.dumps(shadow.get("heuristic_summary", {}), indent=2, ensure_ascii=True))
        print("LLM summary:")
        print(json.dumps(shadow.get("llm_summary", {}), indent=2, ensure_ascii=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
