"""Keep this repository's kernel build opt-in at the GitHub trigger boundary."""

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / "kernel-build.yml"


class WorkflowContractTests(unittest.TestCase):
    def test_kernel_workflow_is_manual_only(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        trigger = re.search(r"(?ms)^on:\n(?P<body>.*?)(?=^[^ \n#][^\n]*:|\Z)", content)
        self.assertIsNotNone(trigger)
        events = re.findall(r"^  ([a-z_]+):", trigger.group("body"), re.M)
        self.assertEqual(events, ["workflow_dispatch"])
        self.assertNotIn("android_version:", trigger.group("body"))
        self.assertNotIn("kernel_version:", trigger.group("body"))

    def test_checksums_are_prepared_before_upload(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(content.index("sha256sum Image *AnyKernel3.zip"),
                        content.index("- name: Upload artifacts"))
        self.assertIn("contents: read", content)
        self.assertIn("contents: write", content)


if __name__ == "__main__":
    unittest.main()
