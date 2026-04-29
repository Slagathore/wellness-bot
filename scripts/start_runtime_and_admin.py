from __future__ import annotations

"""
Convenience launcher to run the modular runtime and admin API together.

This keeps both subprocesses attached to the current terminal; Ctrl+C stops both.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_REPO_TEMP_ROOT = _REPO_ROOT / "wellness_data" / "tmp"


def _default_temp_root() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "wellness-bot" / "tmp"

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "wellness-bot" / "tmp"

    return Path.home() / ".cache" / "wellness-bot" / "tmp"


def _is_legacy_repo_temp_root(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        resolved = path.expanduser().resolve(strict=False)
        legacy = _LEGACY_REPO_TEMP_ROOT.resolve(strict=False)
    except Exception:
        return False
    return resolved == legacy or legacy in resolved.parents


def _configure_temp_env(env: dict[str, str]) -> str:
    configured = env.get("WELLNESS_TEMP_DIR")
    fallback_root = _default_temp_root()
    configured_path = Path(configured).expanduser() if configured else None
    if _is_legacy_repo_temp_root(configured_path):
        print(f"[stack] Ignoring legacy repo temp dir: {configured_path}", flush=True)
        configured_path = None
    temp_root = configured_path if configured_path is not None else fallback_root
    temp_root.mkdir(parents=True, exist_ok=True)
    resolved = str(temp_root.resolve())
    env["WELLNESS_TEMP_DIR"] = resolved
    env["TEMP"] = resolved
    env["TMP"] = resolved
    env["TMPDIR"] = resolved
    return resolved


def main() -> None:
    env = os.environ.copy()
    admin_port = env.get("ADMIN_PORT", "8200")
    temp_root = _configure_temp_env(env)

    processes = []
    cmds = [
        [sys.executable, "-m", "app.main_modular"],
        [sys.executable, "-m", "app.interfaces.admin.server", "--port", admin_port],
    ]

    print(f"[stack] Starting runtime and admin (admin port {admin_port}) ...")
    print(f"[stack] temp dir: {temp_root}")
    try:
        for cmd in cmds:
            print(f"[stack] launching: {' '.join(cmd)}")
            processes.append(subprocess.Popen(cmd, env=env))

        print("[stack] All services started. Press Ctrl+C to stop both.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[stack] Stopping...")
    finally:
        for proc in processes:
            if proc.poll() is None:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        print("[stack] Shutdown complete.")


if __name__ == "__main__":
    main()
