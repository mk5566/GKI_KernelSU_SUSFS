"""Keep the ishtar CI manual, deterministic, and single-profile."""

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / "kernel-build.yml"


class WorkflowContractTests(unittest.TestCase):
    def test_kernel_workflow_is_manual_only_and_allows_ksu_selection(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        trigger = re.search(r"(?ms)^on:\n(?P<body>.*?)(?=^[^ \n#][^\n]*:|\Z)", content)
        self.assertIsNotNone(trigger)
        events = re.findall(r"^  ([a-z_]+):", trigger.group("body"), re.M)
        self.assertEqual(events, ["workflow_dispatch"])
        self.assertIn("ksu_version:", trigger.group("body"))
        self.assertIn("Stable(standard)", trigger.group("body"))
        self.assertIn("Dev(development)", trigger.group("body"))

    def test_fixed_ishtar_profile_is_used_for_validation_and_build(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("KSU_VERSION: ${{ github.event.inputs.ksu_version || 'Stable(standard)' }}", content)
        self.assertEqual(content.count('--ksu-version "${KSU_VERSION}"'), 2)
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
        self.assertIn("Image final.config Module.symvers build.log", content)
        self.assertIn("final.config Module.symvers build.log", content)
        self.assertLess(content.index("sha256sum Image *AnyKernel3.zip"),
                        content.index("- name: Upload artifacts"))


if __name__ == "__main__":
    unittest.main()
