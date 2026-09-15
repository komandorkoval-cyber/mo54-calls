from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mo54_agent.media import find_media_tool


class MediaToolTests(unittest.TestCase):
    def test_uses_agent_specific_directory_without_global_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
            executable.write_bytes(b"")
            with patch("mo54_agent.media.shutil.which", return_value=None), patch.dict(os.environ, {"MO54_AGENT_FFMPEG_DIR": directory}, clear=False):
                self.assertEqual(find_media_tool("ffprobe"), str(executable))
