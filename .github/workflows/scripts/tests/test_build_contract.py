import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder


class BuildContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builder = KernelBuilder(
            BuildConfig(sub_level="211", os_patch_level="2026-09"), self.temp.name
        )
        self.common = self.builder.work_dir / "common"
        self.common.mkdir()
        files = {
            "build.config.gki": 'POST_DEFCONFIG_CMDS="check_defconfig"\n',
            "build.config.aarch64": "GKI_MODULES_LIST=android/gki_aarch64_modules\n",
            "build.config.gki.aarch64": (
                "ABI_DEFINITION=android/abi_gki_aarch64.xml\n"
                "KMI_SYMBOL_LIST=android/abi_gki_aarch64\n"
                "android/abi_gki_aarch64_qcom\n"
                "android/abi_gki_aarch64_xiaomi\n"
                "KMI_ENFORCED=1\nBUILD_SYSTEM_DLKM=1\n"
                "MODULES_LIST=android/gki_system_dlkm_modules\n"
                "MODULES_ORDER=android/gki_aarch64_modules\n"
            ),
        }
        subprocess.run(["git", "init", "-q", str(self.common)], check=True)
        for name, content in files.items():
            (self.common / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.common), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.common), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
            check=True,
        )

    def test_upstream_contract_is_accepted(self):
        self.builder._verify_build_contract()

    def test_reused_workspace_with_disabled_check_is_rejected(self):
        path = self.common / "build.config.gki"
        path.write_text('POST_DEFCONFIG_CMDS=' + '""\n', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "differs from the synced"):
            self.builder._verify_build_contract()


if __name__ == "__main__":
    unittest.main()
