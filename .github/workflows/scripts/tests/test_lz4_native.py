"""Compile/run vendored code on the host, optionally with sanitizers."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import REPO_ROOT


class LZ4NativeTests(unittest.TestCase):
    def test_private_codec_exposes_no_unscoped_global_symbols(self):
        source_dir = REPO_ROOT / "patches/lz4/source"
        header = (source_dir / "lz4.h").read_text(encoding="utf-8")
        source = (source_dir / "lz4.c").read_text(encoding="utf-8")
        declared_static = set(re.findall(
            r"(?:LZ4LIB_API|LZ4LIB_STATIC_API)\s+[\w\s\*]+?\b(\w+)\s*\(", header))
        lines = source.splitlines()
        leaked = []
        for idx, line in enumerate(lines, 1):
            if line.startswith((" ", "\t", "#", "/", "*", "typedef", "struct", "union", "enum",
                                "return", "if", "else", "while", "for", "switch")):
                continue
            match = re.match(r"^([A-Za-z_][\w\s\*]*?)\b(\w+)\s*\([^;]*$", line)
            if not match:
                continue
            prefix, name = match.group(1), match.group(2)
            context = " ".join((
                lines[idx - 3] if idx >= 3 else "",
                lines[idx - 2] if idx >= 2 else "",
                prefix,
            ))
            if ("static" not in context and "LZ4LIB" not in context
                    and "LZ4_FORCE_INLINE" not in context
                    and "LZ4_FORCE_NOINLINE" not in context
                    and name not in declared_static):
                leaked.append(f"line {idx}: {name}")
        self.assertEqual(leaked, [], "Private LZ4 functions must have static linkage")

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
            command = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Wmissing-prototypes", "-Werror",
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
