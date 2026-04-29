from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path


def main() -> None:
    start = time.monotonic()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = Path("scripts") / "test_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = reports_dir / f"pytest_{timestamp}.log"

    print("[tests] Running pytest ...")
    proc = subprocess.run(["pytest"], capture_output=True, text=True)
    log_content = proc.stdout + "\n" + proc.stderr
    log_path.write_text(log_content, encoding="utf-8")

    duration = time.monotonic() - start
    wait_remaining = max(0, 600 - duration)
    if wait_remaining:
        print(f"[tests] Waiting {wait_remaining:.1f}s before reporting ...")
        time.sleep(wait_remaining)

    summary = (
        f"Pytest exit code: {proc.returncode}\n"
        f"Duration: {duration:.2f}s\n"
        f"Log saved to: {log_path}"
    )
    print("[tests] Report\n" + summary)


if __name__ == "__main__":
    main()
