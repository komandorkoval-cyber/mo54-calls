from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import POINTER, Structure, byref, c_bool, c_byte, c_uint32, c_void_p, c_wchar_p, cast, windll
from pathlib import Path

from .errors import AgentError


KEYRING_SERVICE = "MO54CallsAgent"
CRM_TOKEN_ACCOUNT = "crm-transcript-token"


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
