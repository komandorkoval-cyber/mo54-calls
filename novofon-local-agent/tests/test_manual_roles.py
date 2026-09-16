from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mo54_agent.cli import _apply_manual_pilot_roles
from mo54_agent.errors import AgentError
from mo54_agent.review import automatic_baseline_path


class ManualRoleAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pilot.json"
        self.path.write_text(json.dumps({
            "text": "local only",
            "asr_model": "test",
            "language": "ru",
            "segments": [
                {"ordinal": 1, "started_ms": 0, "ended_ms": 1000, "role": "unknown", "text": "one", "speaker_label": "Спикер 1"},
                {"ordinal": 2, "started_ms": 1000, "ended_ms": 2000, "role": "unknown", "text": "two", "speaker_label": "Спикер 1"},
                {"ordinal": 3, "started_ms": 2000, "ended_ms": 3000, "role": "unknown", "text": "three", "speaker_label": "Спикер 2"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_applies_operator_confirmed_roles_without_changing_text(self) -> None:
        counts = _apply_manual_pilot_roles(
            self.path,
            manager_labels={"Спикер 1"},
            customer_labels={"Спикер 2"},
        )

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(counts, {"manager": 2, "customer": 1})
        self.assertEqual(raw["text"], "local only")
        self.assertEqual([segment["role"] for segment in raw["segments"]], ["manager", "manager", "customer"])
        self.assertEqual(raw["manual_role_assignment"]["method"], "operator_confirmation")
        self.assertEqual(raw["manual_role_assignment"]["manager_labels"], ["Спикер 1"])
        baseline = json.loads(automatic_baseline_path(self.path).read_text(encoding="utf-8"))
        self.assertEqual([segment["role"] for segment in baseline["segments"]], ["unknown", "unknown", "unknown"])

    def test_rejects_unknown_or_conflicting_labels(self) -> None:
        with self.assertRaises(AgentError) as unknown:
            _apply_manual_pilot_roles(self.path, manager_labels={"Нет такого"}, customer_labels=set())
        self.assertEqual(unknown.exception.code, "manual_role_label_unknown")

        with self.assertRaises(AgentError) as conflict:
            _apply_manual_pilot_roles(self.path, manager_labels={"Спикер 1"}, customer_labels={"Спикер 1"})
        self.assertEqual(conflict.exception.code, "manual_roles_conflict")


if __name__ == "__main__":
    unittest.main()
