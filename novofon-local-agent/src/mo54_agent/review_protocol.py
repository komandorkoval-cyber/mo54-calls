from __future__ import annotations

import sys
from pathlib import Path

from .errors import AgentError
from .review import REVIEW_URI_SCHEME


REVIEW_URI_REGISTRY_PATH = rf"Software\Classes\{REVIEW_URI_SCHEME}"


def review_uri_command(agent_executable: Path) -> str:
    """Return the exact command stored in the current user's URI handler."""

    return f'"{agent_executable}" review-uri "%1"'


def install_review_uri_handler(agent_executable: Path | None = None) -> Path:
    """Register only the current user's one-click local review handler.

    This has no relationship to the scheduled agent: it does not start a
    browser, touch models, create a task, or contact CRM.  The executable is
    passed the whole URI and validates it again before it can open a review.
    """

    if sys.platform != "win32":
        raise AgentError("review_uri_windows_required", "The local review handler requires Windows", retryable=False)
    executable = (agent_executable or Path(sys.executable).with_name("mo54-agent.exe")).resolve()
    if not executable.is_file():
        raise AgentError("review_uri_agent_missing", "The installed mo54-agent executable was not found", retryable=False)
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REVIEW_URI_REGISTRY_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:MO54 Calls local review")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        command_path = rf"{REVIEW_URI_REGISTRY_PATH}\shell\open\command"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, command_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, review_uri_command(executable))
    except OSError as exc:
        raise AgentError("review_uri_registration_failed", "Could not register the local review link", retryable=False) from exc
    return executable
