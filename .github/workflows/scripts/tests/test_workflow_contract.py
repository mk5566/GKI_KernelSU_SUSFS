"""Keep the ishtar CI manual, deterministic, and single-profile."""

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / "kernel-build.yml"


class WorkflowContractTests(unittest.TestCase):
    def test_kernel_workflow_is_manual_only_and_has_no_inputs(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        trigger = re.search(r"(?ms)^on:\n(?P<body>.*?)(?=^[^ \n#][^\n]*:|\Z)", content)
        self.assertIsNotNone(trigger)
        events = re.findall(r"^  ([a-z_]+):", trigger.group("body"), re.M)
        self.assertEqual(events, ["workflow_dispatch"])
        self.assertNotIn("inputs:", trigger.group("body"))
        self.assertNotIn("github.event.inputs", content)

    def test_fixed_ishtar_profile_is_used_for_validation_and_build(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("FIXED_KSU_VERSION: 'Stable(standard)'", content)
        self.assertEqual(content.count('--ksu-version "${FIXED_KSU_VERSION}"'), 2)
        self.assertEqual(content.count("--no-zram"), 2)
        self.assertEqual(content.count("--bbr"), 2)
        self.assertEqual(content.count('--optional-patches=""'), 2)
        self.assertEqual(content.count("--no-release"), 2)

    def test_workflow_only_builds_and_uploads(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("contents: read", content)
        self.assertNotIn("contents: write", content)
        self.assertNotIn("Send Telegram", content)
        self.assertNotIn("gh release", content)
        self.assertNotIn("\n  release:\n", content)
        self.assertLess(content.index("sha256sum Image *AnyKernel3.zip"),
                        content.index("- name: Upload artifacts"))


if __name__ == "__main__":
    unittest.main()
