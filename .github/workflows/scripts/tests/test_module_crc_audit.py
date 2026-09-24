"""Verify symbol CRC parsing without depending on a connected phone."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module_crc_audit import AuditError, read_symvers, read_versions


def fixture_module(crc=0x0222DD63):
    blob = bytearray(0x300)
    blob[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", blob, 18, 183)  # AArch64
    struct.pack_into("<Q", blob, 40, 0x100)  # section headers
    struct.pack_into("<HHH", blob, 58, 64, 4, 1)
    names = b"\0.shstrtab\0__versions\0"
    blob[0x200:0x200 + len(names)] = names
    struct.pack_into("<IIQQQQIIQQ", blob, 0x140, 1, 3, 0, 0,
                     0x200, len(names), 0, 0, 1, 0)
    struct.pack_into("<IIQQQQIIQQ", blob, 0x180, names.index(b"__versions"),
                     1, 0, 0, 0x240, 64, 0, 0, 8, 0)
    struct.pack_into("<IIQQQQIIQQ", blob, 0x1c0, 0, 8, 0, 0,
                     0x1000, 0x100, 0, 0, 8, 0)  # NOBITS may exceed file size.
    struct.pack_into("<Q", blob, 0x240, crc)
    blob[0x248:0x248 + len(b"module_layout")] = b"module_layout"
    return bytes(blob)


class ModuleCRCAuditTests(unittest.TestCase):
    def test_reads_aarch64_modversions(self):
        self.assertEqual(read_versions(fixture_module()),
                         {"module_layout": 0x0222DD63})

    def test_rejects_missing_or_truncated_versions(self):
        with self.assertRaises(AuditError):
            read_versions(fixture_module()[:0x250])
        with self.assertRaises(AuditError):
            read_versions(b"not an ELF")

    def test_reads_vmlinux_exports_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Module.symvers"
            path.write_text("0x0222dd63\tmodule_layout\tvmlinux\tEXPORT_SYMBOL\t\n"
                            "0x12345678\tvendor_symbol\tvendor.ko\tEXPORT_SYMBOL\t\n")
            self.assertEqual(read_symvers(path), {"module_layout": 0x0222DD63})


if __name__ == "__main__":
    unittest.main()
