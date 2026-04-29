"""Security-related helpers shared across runtimes."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
import tkinter as tk
from contextlib import suppress
from pathlib import Path
from tkinter import messagebox, simpledialog

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("wellness.security")

_FNAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename_component(
    component: str | None, fallback: str = "data", max_length: int = 80
) -> str:
    """Return a filesystem-safe filename component."""
    if component is None:
        return fallback
    cleaned = _FNAME_CLEAN_RE.sub("_", str(component))
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


def load_encrypted_env() -> None:
    """Load environment variables from an encrypted .env file when available."""
    encrypted_path = Path(".env.encrypted")
    key_path = Path(".env.key")
    if not encrypted_path.exists() or not key_path.exists():
        return
    try:
        from cryptography.fernet import Fernet  # type: ignore[import]
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning(
            "Encrypted .env detected but 'cryptography' is not installed; skipping."
        )
        return
    try:
        key = key_path.read_text(encoding="utf-8").strip().encode("utf-8")
        cipher = Fernet(key)
        decrypted = cipher.decrypt(encrypted_path.read_bytes()).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to decrypt .env.encrypted: %s", exc)
        security_logger.error("Failed to decrypt .env.encrypted: %s", exc)
        return
    for line in decrypted.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key_name, value = line.split("=", 1)
        os.environ.setdefault(key_name.strip(), value.strip())
    logger.info("Loaded encrypted environment variables.")
    security_logger.info("Loaded encrypted environment variables.")


class AdminAuth:
    """Password gate for the admin console."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        max_attempts: int = 3,
        filename: str = "admin_password.hash",
    ) -> None:
        self.storage_dir = Path(data_root)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.hash_file = self.storage_dir / filename
        self.max_attempts = max_attempts
        self._skip_auth_flag = self.storage_dir / "skip_admin_auth.flag"

    def authenticate(self, root: tk.Misc) -> bool:
        if self._skip_auth_flag.exists():
            fresh_token = False
            try:
                age = time.time() - self._skip_auth_flag.stat().st_mtime
                fresh_token = age <= 300  # 5-minute window
            except Exception:  # noqa: BLE001
                fresh_token = False
            with suppress(Exception):
                self._skip_auth_flag.unlink()
            if fresh_token:
                logger.info("Admin login skipped due to trusted restart token.")
                security_logger.info(
                    "Admin login skipped due to trusted restart token."
                )
                return True
            logger.warning(
                "Stale restart token detected; proceeding with normal authentication."
            )

        if not self._ensure_password_initialized(root):
            return False

        for attempt in range(1, self.max_attempts + 1):
            password = simpledialog.askstring(
                "Admin Login",
                "Enter admin password:",
                show="*",
                parent=root,
            )
            if password is None:
                messagebox.showwarning(
                    "Access Denied", "Admin authentication cancelled."
                )
                security_logger.warning("Admin login cancelled by operator.")
                return False
            if self._verify_password(password.strip()):
                logger.info("Admin login successful.")
                security_logger.info("Admin login successful.")
                return True

            remaining = self.max_attempts - attempt
            if remaining:
                messagebox.showerror(
                    "Invalid Password",
                    f"Incorrect password. {remaining} attempt(s) remaining.",
                )
            else:
                messagebox.showerror("Access Denied", "Too many failed attempts.")
                logger.warning("Admin login failed: maximum attempts exceeded.")
                security_logger.warning(
                    "Admin login failed: maximum attempts exceeded."
                )
        return False

    def _ensure_password_initialized(self, root: tk.Misc) -> bool:
        if self.hash_file.exists():
            return True

        env_password = os.environ.get("WELLNESS_ADMIN_PASSWORD")
        if env_password:
            self._store_password(env_password.strip())
            security_logger.info("Admin password seeded from WELLNESS_ADMIN_PASSWORD.")
            return True

        for _ in range(3):
            password = simpledialog.askstring(
                "Set Admin Password",
                "Create an admin password:",
                show="*",
                parent=root,
            )
            if not password:
                messagebox.showwarning(
                    "Setup Required", "Admin password setup cancelled."
                )
                security_logger.warning("Admin password setup cancelled during prompt.")
                continue

            confirm = simpledialog.askstring(
                "Confirm Admin Password",
                "Re-enter the new admin password:",
                show="*",
                parent=root,
            )
            if password != confirm:
                messagebox.showerror("Mismatch", "Passwords did not match. Try again.")
                continue

            self._store_password(password.strip())
            security_logger.info("Admin password initialized.")
            return True

        messagebox.showerror("Setup Failed", "Unable to set admin password. Exiting.")
        security_logger.error("Admin password setup failed after multiple attempts.")
        return False

    def _store_password(self, password: str) -> None:
        salt = secrets.token_hex(16)
        hashed = self._hash_password(password, salt)
        self.hash_file.write_text(f"{salt}:{hashed}\n", encoding="utf-8")

    def _verify_password(self, password: str) -> bool:
        try:
            raw = self.hash_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.error("Failed to read admin password hash: %s", exc)
            security_logger.error("Failed to read admin password hash: %s", exc)
            return False

        if ":" not in raw:
            logger.error("Admin password hash file is malformed.")
            security_logger.error("Admin password hash file is malformed.")
            return False

        salt, stored_hash = raw.split(":", 1)
        return self._hash_password(password, salt) == stored_hash

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


__all__ = ["AdminAuth", "sanitize_filename_component", "load_encrypted_env"]
