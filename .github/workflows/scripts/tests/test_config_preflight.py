"""Exercise the AOSP entry point and post-defconfig boundary in a real shell."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder
import test_build_modes as build_modes


def find_bash():
    bash = shutil.which("bash")
    if not bash and os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
        if candidate and candidate.is_file():
            bash = str(candidate)
    return bash


@unittest.skipUnless(find_bash(), "requires Bash (Git Bash is supported on Windows)")
class ConfigPreflightTests(unittest.TestCase):
    # Minimal AOSP build.sh contract: source the build config and its fragment,
    # initialize the selected tools, generate DEFCONFIG, run the hook, compile.
    BUILD_SH = """set -e
test "$BUILD_CONFIG" = common/build.config.gki.aarch64
test "$INSTALL_MOD_STRIP" = 1
KERNEL_DIR=common
OUT_DIR="$PWD/out/android13-5.15/common"
TOOL_ARGS=LLVM=1
source "$BUILD_CONFIG"
source "$GKI_BUILD_CONFIG_FRAGMENT"
make() {
    test "$1" = LLVM=1 || test "$1" = -C
    if [[ "$*" == *gki_defconfig* ]]; then
        mkdir -p "$OUT_DIR"
        cp config.fixture "$OUT_DIR/.config"
        if [[ "$(pahole --version)" == v1.18 ]]; then
            sed -i 's/CONFIG_DEBUG_INFO_BTF_MODULES=y/# CONFIG_DEBUG_INFO_BTF_MODULES is not set/' "$OUT_DIR/.config"
        fi
    elif [[ "$*" == *olddefconfig* ]]; then
        return "${GKI_TEST_OLDDEFCONFIG_STATUS:-0}"
    else
        return 9
    fi
}
make ${TOOL_ARGS} gki_defconfig
eval ${POST_DEFCONFIG_CMDS}
touch compiled
"""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.builder = KernelBuilder(BuildConfig(), directory.name)
        root = self.builder.work_dir
        paths = {
            "build/build.sh": self.BUILD_SH,
            "common/build.config.gki.aarch64":
                'PATH="$PWD/build/kernel/build-tools/path/linux-x86:$PATH"\n',
            "build/kernel/build-tools/path/linux-x86/pahole": "#!/bin/bash\necho v1.24\n",
            "host/pahole": "#!/bin/bash\necho v1.18\n",
            "config.fixture": build_modes.RuntimeConfigTests()._valid_config(),
        }
        for name, text in paths.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.builder.env["PATH"] = str(root / "host") + os.pathsep + os.environ["PATH"]
        self.builder.configure_build_fragment()
        self.fragment = Path(self.builder.env["GKI_BUILD_CONFIG_FRAGMENT"])
        self.original_fragment = self.fragment.read_bytes()
        self.original_env = self.builder.env.copy()
        run = subprocess.run

        def run_bash(args, **kwargs):
            # Execute the production invocation; adapt only Windows path syntax.
            self.assertEqual(args, ["bash", "build/build.sh"])
            if os.name == "nt":
                env = kwargs["env"].copy()
                env["GKI_BUILD_CONFIG_FRAGMENT"] = Path(env["GKI_BUILD_CONFIG_FRAGMENT"]).as_posix()
                # Git Bash receives the Windows PATH automatically. Supply the
                # fixture host tool in shell syntax so host fallback is tested.
                args = [find_bash(), "-c",
                        'export PATH="$PWD/host:$PATH"; exec bash build/build.sh']
                kwargs["env"] = env
            else:
                args = [find_bash(), *args[1:]]
            return run(args, **kwargs)

        mock = patch("kernel_builder.subprocess.run", side_effect=run_bash)
        mock.start()
        self.addCleanup(mock.stop)

    def test_preflight_uses_certified_pahole_and_exits_before_compilation(self):
        self.builder.generate_and_verify_config()
        self.assertEqual(self.builder.config_checks["precompile"]["actual"]
                         ["CONFIG_DEBUG_INFO_BTF_MODULES"], "y")
        self.assertFalse((self.builder.work_dir / "compiled").exists())
        self.assertEqual(self.fragment.read_bytes(), self.original_fragment)
        self.assertEqual(self.builder.env, self.original_env)
        # The exit hook is local to preflight; the next build must compile.
        self.assertEqual(self.builder._run_kernel_build().returncode, 0)
        self.assertTrue((self.builder.work_dir / "compiled").is_file())

    def test_failed_olddefconfig_cannot_validate_stale_config_or_compile(self):
        self.builder.env["GKI_TEST_OLDDEFCONFIG_STATUS"] = "23"
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.builder.generate_and_verify_config()
        self.assertEqual(failure.exception.returncode, 23)
        self.assertEqual(self.builder.config_checks, {})
        self.assertFalse((self.builder.work_dir / "compiled").exists())

    def test_incapable_host_pahole_still_fails_the_required_btf_check(self):
        (self.builder.work_dir / "build/kernel/build-tools/path/linux-x86/pahole").unlink()
        with self.assertRaisesRegex(RuntimeError, "CONFIG_DEBUG_INFO_BTF_MODULES=n, expected y"):
            self.builder.generate_and_verify_config()
        self.assertEqual(self.builder.config_checks, {})
        self.assertFalse((self.builder.work_dir / "compiled").exists())


if __name__ == "__main__":
    unittest.main()
