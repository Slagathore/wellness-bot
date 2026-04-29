from __future__ import annotations

import os
import sys
from types import ModuleType
from typing import Any, cast

import app.utils.security as security
from app.utils.security import (
    AdminAuth,
    load_encrypted_env,
    sanitize_filename_component,
)


def test_sanitize_filename_component_basic():
    fn = sanitize_filename_component("Hello World!.txt")
    assert fn == "Hello_World_.txt"


def test_sanitize_filename_component_fallback():
    fn = sanitize_filename_component("..///", fallback="safe", max_length=10)
    assert fn == "safe"
    assert sanitize_filename_component("", fallback="default") == "default"


def test_load_encrypted_env_with_stub(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").unlink(missing_ok=True)

    key_path = tmp_path / ".env.key"
    key_path.write_text("fake-key", encoding="utf-8")

    payload = b"FOO=bar\nBAZ = qux"
    (tmp_path / ".env.encrypted").write_bytes(payload)

    fake_crypto = ModuleType("cryptography")
    fake_fernet_module = ModuleType("cryptography.fernet")

    class FakeFernet:
        def __init__(self, key: bytes):
            assert key == b"fake-key"

        def decrypt(self, data: bytes) -> bytes:
            assert data == payload
            return payload

    setattr(fake_fernet_module, "Fernet", FakeFernet)
    setattr(fake_crypto, "fernet", fake_fernet_module)

    monkeypatch.setitem(sys.modules, "cryptography", fake_crypto)
    monkeypatch.setitem(sys.modules, "cryptography.fernet", fake_fernet_module)

    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAZ", raising=False)

    load_encrypted_env()

    assert os.environ["FOO"] == "bar"
    assert os.environ["BAZ"] == "qux"


def test_load_encrypted_env_without_module(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.key").write_text("fake", encoding="utf-8")
    (tmp_path / ".env.encrypted").write_bytes(b"data")

    monkeypatch.delitem(sys.modules, "cryptography", raising=False)
    monkeypatch.delitem(sys.modules, "cryptography.fernet", raising=False)

    monkeypatch.delenv("TEST_SECRET", raising=False)

    load_encrypted_env()

    assert "TEST_SECRET" not in os.environ


def test_admin_auth_setup_and_login(monkeypatch, tmp_path):
    auth = AdminAuth(tmp_path)
    root = cast(Any, object())

    responses = iter(["StrongPass123!", "StrongPass123!", "StrongPass123!"])

    monkeypatch.setattr(
        security.simpledialog,
        "askstring",
        lambda *args, **kwargs: next(responses),
    )
    for func in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(security.messagebox, func, lambda *args, **kwargs: None)

    assert auth.authenticate(root)
    assert auth.hash_file.exists()

    monkeypatch.setattr(
        security.simpledialog,
        "askstring",
        lambda *args, **kwargs: "StrongPass123!",
    )

    assert auth.authenticate(root)


def test_admin_auth_login_failures(monkeypatch, tmp_path):
    auth = AdminAuth(tmp_path)
    root = cast(Any, object())
    auth._store_password("Secret123!")

    attempts = iter(["wrong", "stillwrong", "nope"])
    monkeypatch.setattr(
        security.simpledialog,
        "askstring",
        lambda *args, **kwargs: next(attempts),
    )
    for func in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(security.messagebox, func, lambda *args, **kwargs: None)

    assert not auth.authenticate(root)
    assert auth.hash_file.exists()
