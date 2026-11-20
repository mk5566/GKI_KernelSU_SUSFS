"""Compile/run vendored code on the host, optionally with sanitizers."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import REPO_ROOT


class LZ4NativeTests(unittest.TestCase):
    def test_private_freestanding_codec_roundtrips_and_bounded_decoding(self):
        # Match the private integration's Clang compilation model. GCC treats
        # an intentionally unavailable freestanding legacy static API as an
        # undefined-function warning despite its unused attribute.
        compiler = shutil.which("clang")
        if not compiler:
            if os.environ.get("GKI_REQUIRE_NATIVE") == "1":
                self.fail("GKI_REQUIRE_NATIVE=1 requires host Clang")
            self.skipTest("host Clang unavailable")
        with tempfile.TemporaryDirectory(prefix="gki-lz4-test-") as temp:
            root = Path(temp)
            linux = root / "linux"
            linux.mkdir()
            (linux / "types.h").write_text(
                "#pragma once\n#include <stddef.h>\n"
                "typedef signed char s8; typedef unsigned char u8; typedef unsigned short u16;\n"
                "typedef unsigned int u32; typedef int s32; typedef unsigned long long u64;\n")
            (linux / "limits.h").write_text("#include <limits.h>\n")
            command = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                       "-I", str(root), "-I", str(REPO_ROOT / "patches/lz4/source"),
                       str(Path(__file__).parent / "native/lz4_roundtrip.c"), "-o", str(root / "lz4-test")]
            if os.environ.get("GKI_TEST_SANITIZERS") == "1":
                command[2:2] = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                                "-fno-omit-frame-pointer", "-g"]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(root / "lz4-test")], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("3300 roundtrips; 20000", result.stdout)


if __name__ == "__main__":
    unittest.main()
