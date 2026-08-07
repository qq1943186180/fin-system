"""Per-user secret storage backed by Windows Data Protection API (DPAPI).

Release builds never fall back to writing plaintext secrets.  On non-Windows
hosts, or when ``FINANCE_SYSTEM_TEST_MODE=1``, the fallback is process-local
memory only.  This keeps cross-platform tests safe while making unsupported
production persistence explicit.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Final

from app_paths import CREDENTIAL_STORE_PATH, ensure_app_dirs


STORE_VERSION: Final[int] = 1
TEST_MODE_ENV: Final[str] = "FINANCE_SYSTEM_TEST_MODE"
_ENTROPY: Final[bytes] = b"FinanceSystem.credentials.v1"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LOCK = threading.RLock()
_MEMORY_STORE: dict[str, str] = {}


class SecretStoreError(RuntimeError):
    """Raised when an encrypted credential cannot be stored or recovered."""


def _test_mode() -> bool:
    return os.environ.get(TEST_MODE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _memory_backend() -> bool:
    return os.name != "nt" or _test_mode()


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("secret name must contain only letters, digits, '.', '_', ':', or '-'")
    return name


if os.name == "nt":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _make_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


def _protect(data: bytes, description: str) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("persistent secret storage requires Windows DPAPI")
    source, source_buffer = _make_blob(data)
    entropy, entropy_buffer = _make_blob(_ENTROPY)
    output = _DataBlob()
    # Keep both backing buffers alive until the native call returns.
    _buffers = (source_buffer, entropy_buffer)
    if not _crypt32.CryptProtectData(
        ctypes.byref(source),
        description,
        ctypes.byref(entropy),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output),
    ):
        raise SecretStoreError(f"DPAPI encryption failed (Windows error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("persistent secret storage requires Windows DPAPI")
    source, source_buffer = _make_blob(data)
    entropy, entropy_buffer = _make_blob(_ENTROPY)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    # Keep both backing buffers alive until the native call returns.
    _buffers = (source_buffer, entropy_buffer)
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise SecretStoreError(f"DPAPI decryption failed (Windows error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _kernel32.LocalFree(output.pbData)
        if description:
            _kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _read_store() -> dict:
    if not CREDENTIAL_STORE_PATH.exists():
        return {"version": STORE_VERSION, "secrets": {}}
    try:
        payload = json.loads(CREDENTIAL_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretStoreError("encrypted credential store is unreadable") from exc
    if payload.get("version") != STORE_VERSION or not isinstance(payload.get("secrets"), dict):
        raise SecretStoreError("encrypted credential store has an unsupported format")
    return payload


def _write_store(payload: dict) -> None:
    ensure_app_dirs()
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix="credentials-", suffix=".tmp", dir=str(CREDENTIAL_STORE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CREDENTIAL_STORE_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def set_secret(name: str, value: str) -> None:
    """Store *value* under *name* for the current Windows user."""

    name = _validate_name(name)
    if not isinstance(value, str) or not value:
        raise ValueError("secret value must be a non-empty string")
    with _LOCK:
        if _memory_backend():
            _MEMORY_STORE[name] = value
            return
        payload = _read_store()
        protected = _protect(value.encode("utf-8"), f"FinanceSystem:{name}")
        payload["secrets"][name] = base64.b64encode(protected).decode("ascii")
        _write_store(payload)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Return a secret, or *default* when the name is not configured."""

    name = _validate_name(name)
    with _LOCK:
        if _memory_backend():
            return _MEMORY_STORE.get(name, default)
        payload = _read_store()
        protected_text = payload["secrets"].get(name)
        if protected_text is None:
            return default
        try:
            protected = base64.b64decode(protected_text, validate=True)
            return _unprotect(protected).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecretStoreError(f"encrypted secret '{name}' is corrupt") from exc


def delete_secret(name: str) -> bool:
    """Delete *name* and return whether it previously existed."""

    name = _validate_name(name)
    with _LOCK:
        if _memory_backend():
            return _MEMORY_STORE.pop(name, None) is not None
        payload = _read_store()
        existed = payload["secrets"].pop(name, None) is not None
        if existed:
            _write_store(payload)
        return existed


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Manage Finance System DPAPI secrets")
    parser.add_argument("action", choices=("set", "get", "delete", "status"))
    parser.add_argument("name")
    args = parser.parse_args()

    if args.action == "set":
        first = getpass.getpass("Secret (input hidden): ")
        second = getpass.getpass("Confirm secret: ")
        if first != second:
            parser.error("secret values did not match")
        set_secret(args.name, first)
        print(f"Stored '{args.name}' with DPAPI for the current user.")
    elif args.action == "delete":
        print("Deleted." if delete_secret(args.name) else "Not configured.")
    elif args.action == "status":
        print("configured" if get_secret(args.name) is not None else "not configured")
    else:
        # Deliberately never print the secret itself.
        print("configured" if get_secret(args.name) is not None else "not configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
