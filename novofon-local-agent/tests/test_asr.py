from __future__ import annotations

import unittest

from mo54_agent.asr import cosine, role_map_for_speakers


class RoleAssignmentTests(unittest.TestCase):
    def test_confident_manager_assigns_the_only_other_speaker_to_customer(self) -> None:
        self.assertEqual(
            role_map_for_speakers(["A", "B"], {"A": 0.88, "B": 0.20}, 0.72),
            {"A": "manager", "B": "customer"},
        )

    def test_unconfirmed_reference_keeps_honest_unknown_speakers(self) -> None:
        self.assertEqual(
            role_map_for_speakers(["A", "B"], {"A": 0.6, "B": 0.5}, 0.72),
            {"A": "unknown", "B": "unknown"},
        )
        self.assertLess(cosine([1.0, 0.0], [0.0, 1.0]), 0.72)
