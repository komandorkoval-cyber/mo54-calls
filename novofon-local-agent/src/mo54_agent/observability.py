from __future__ import annotations

import base64
import ctypes
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


def safe_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("mo54-agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def notify(title: str, code: str) -> None:
    """Show a Windows notification containing only an operational error code."""
    if sys.platform != "win32":
        return
    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{title}</text><text>{code}</text>"
        "</binding></visual></toast>"
    )
    encoded = base64.b64encode(
        (
            "$xml=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]::new();"
            f"$xml.LoadXml('{xml}');"
            "$toast=[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]::new($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]::"
            "CreateToastNotifier('MO54 Calls Agent').Show($toast)"
        ).encode("utf-16le")
    ).decode("ascii")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


@contextmanager
def prevent_sleep():
    """Request awake state only while the caller actively downloads or runs ASR."""
    if sys.platform != "win32":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    continuous = 0x80000000
    system_required = 0x00000001
    try:
        kernel32.SetThreadExecutionState(continuous | system_required)
        yield
    finally:
        kernel32.SetThreadExecutionState(continuous)
