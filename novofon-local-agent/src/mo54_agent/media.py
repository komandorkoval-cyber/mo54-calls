from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_media_tool(name: str) -> str | None:
    """Resolve ffmpeg/ffprobe without requiring a machine-wide PATH edit.

    Winget's user-scoped Gyan package intentionally does not always update the
    current process PATH.  The agent therefore recognizes only its documented
    local package layout, plus an optional agent-specific directory, and never
    scans arbitrary disks or changes global environment settings.
    """
    on_path = shutil.which(name)
    if on_path:
        return on_path

    extension = ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else ""
    executable = f"{name}{extension}"
    configured = os.environ.get("MO54_AGENT_FFMPEG_DIR", "").strip()
    roots = [Path(configured)] if configured else []

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        roots.extend(sorted(package_root.glob("Gyan.FFmpeg.*"), reverse=True))

    for root in roots:
        direct = root / executable
        if direct.is_file():
            return str(direct)
        for candidate in root.glob(f"**/bin/{executable}"):
            if candidate.is_file():
                return str(candidate)
    return None
