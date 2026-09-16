from __future__ import annotations

import base64
import ctypes
import json
import secrets
import sys
from ctypes import POINTER, Structure, byref, c_bool, c_byte, c_uint32, c_void_p, c_wchar_p, cast, windll
from pathlib import Path

from .errors import AgentError


KEYRING_SERVICE = "MO54CallsAgent"
CRM_TOKEN_ACCOUNT = "crm-transcript-token"
# This is deliberately separate from the server credential.  It authenticates
# a local operator-approved review before that review can be delivered.
REVIEW_SEAL_ACCOUNT = "review-artifact-seal-v1"
REVIEW_SEAL_KEY_BYTES = 32


def set_crm_token(token: str) -> None:
    if sys.platform != "win32":
        raise AgentError("credential_manager_unavailable", "CRM token storage requires Windows Credential Manager", retryable=False)
    if len(token.strip()) < 32:
        raise AgentError("crm_token_invalid", "CRM token must be at least 32 characters", retryable=False)
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, CRM_TOKEN_ACCOUNT, token.strip())
    except ImportError as exc:
        raise AgentError("credential_manager_unavailable", "Windows Credential Manager integration is unavailable", retryable=False) from exc


def get_crm_token() -> str:
    if sys.platform != "win32":
        raise AgentError("credential_manager_unavailable", "CRM token storage requires Windows Credential Manager", retryable=False)
    try:
        import keyring
        token = keyring.get_password(KEYRING_SERVICE, CRM_TOKEN_ACCOUNT)
    except ImportError as exc:
        raise AgentError("credential_manager_unavailable", "Windows Credential Manager integration is unavailable", retryable=False) from exc
    if not token:
        raise AgentError("crm_token_missing", "CRM token is missing from Windows Credential Manager", retryable=False)
    return token


def _review_seal_key(*, create: bool) -> bytes:
    """Load a local-only HMAC key from Windows Credential Manager.

    The approved review file is intentionally readable JSON for an operator,
    so a plain SHA-256 digest cannot establish immutability: an editor could
    recompute it.  A separate Credential Manager secret provides an integrity
    boundary without ever sending the key to CRM or storing it beside audio.
    """

    if sys.platform != "win32":
        raise AgentError(
            "pilot_review_seal_unavailable",
            "Approved review sealing requires Windows Credential Manager",
            retryable=False,
        )
    try:
        import keyring
        encoded = keyring.get_password(KEYRING_SERVICE, REVIEW_SEAL_ACCOUNT)
    except Exception as exc:
        raise AgentError(
            "pilot_review_seal_unavailable",
            "Windows Credential Manager review seal is unavailable",
            retryable=False,
        ) from exc
    if encoded:
        try:
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise AgentError(
                "pilot_review_seal_invalid",
                "The local review seal is unreadable",
                retryable=False,
            ) from exc
        if len(key) != REVIEW_SEAL_KEY_BYTES:
            raise AgentError(
                "pilot_review_seal_invalid",
                "The local review seal has an invalid length",
                retryable=False,
            )
        return key
    if not create:
        raise AgentError(
            "pilot_review_seal_missing",
            "The local review seal is missing; delivery is blocked",
            retryable=False,
        )
    key = secrets.token_bytes(REVIEW_SEAL_KEY_BYTES)
    try:
        keyring.set_password(
            KEYRING_SERVICE,
            REVIEW_SEAL_ACCOUNT,
            base64.urlsafe_b64encode(key).decode("ascii"),
        )
    except Exception as exc:
        raise AgentError(
            "pilot_review_seal_unavailable",
            "Windows Credential Manager cannot store the local review seal",
            retryable=False,
        ) from exc
    return key


def get_or_create_review_seal_key() -> bytes:
    """Create the local review integrity key only while approving a review."""

    return _review_seal_key(create=True)


def get_review_seal_key() -> bytes:
    """Load, but never silently replace, the key required for delivery."""

    return _review_seal_key(create=False)


if sys.platform == "win32":
    class DATA_BLOB(Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", POINTER(c_byte))]


    _crypt_protect = windll.crypt32.CryptProtectData
    _crypt_protect.argtypes = [POINTER(DATA_BLOB), c_wchar_p, POINTER(DATA_BLOB), c_void_p, c_void_p, c_uint32, POINTER(DATA_BLOB)]
    _crypt_protect.restype = c_bool
    _crypt_unprotect = windll.crypt32.CryptUnprotectData
    _crypt_unprotect.argtypes = [POINTER(DATA_BLOB), POINTER(c_wchar_p), POINTER(DATA_BLOB), c_void_p, c_void_p, c_uint32, POINTER(DATA_BLOB)]
    _crypt_unprotect.restype = c_bool
    _local_free = windll.kernel32.LocalFree
    _local_free.argtypes = [c_void_p]
    _local_free.restype = c_void_p


    def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array[c_byte]]:
        buffer = (c_byte * len(data)).from_buffer_copy(data)
        return DATA_BLOB(len(data), cast(buffer, POINTER(c_byte))), buffer


    def _protect(payload: bytes) -> bytes:
        source, _buffer = _blob(payload)
        result = DATA_BLOB()
        if not _crypt_protect(byref(source), c_wchar_p("MO54 Calls manager embedding"), None, None, None, 0, byref(result)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            _local_free(cast(result.pbData, c_void_p))


    def _unprotect(payload: bytes) -> bytes:
        source, _buffer = _blob(payload)
        result = DATA_BLOB()
        if not _crypt_unprotect(byref(source), None, None, None, None, 0, byref(result)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            _local_free(cast(result.pbData, c_void_p))


def save_manager_embedding(path: Path, vector: list[float]) -> None:
    if sys.platform != "win32":
        raise AgentError("dpapi_unavailable", "Manager embedding may only be stored with Windows DPAPI", retryable=False)
    if not vector:
        raise AgentError("manager_embedding_empty", "Manager voice reference did not produce an embedding", retryable=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _protect(json.dumps({"version": 1, "embedding": vector}, separators=(",", ":")).encode("utf-8"))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(base64.b64encode(protected).decode("ascii"), encoding="ascii")
    temporary.replace(path)


def load_manager_embedding(path: Path) -> list[float] | None:
    if not path.exists():
        return None
    if sys.platform != "win32":
        raise AgentError("dpapi_unavailable", "Manager embedding requires Windows DPAPI", retryable=False)
    try:
        payload = _unprotect(base64.b64decode(path.read_text(encoding="ascii")))
        parsed = json.loads(payload)
        vector = parsed["embedding"]
        if not isinstance(vector, list) or not vector or not all(isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding shape")
        return [float(value) for value in vector]
    except Exception as exc:
        raise AgentError("manager_embedding_unreadable", "Manager voice reference cannot be decrypted", retryable=False) from exc
