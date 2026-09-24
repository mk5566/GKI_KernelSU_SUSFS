"""Compare AArch64 vendor module CRCs with a built kernel's Module.symvers.

ADB mode reads module bytes into memory. It never writes raw device captures.
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
from pathlib import Path


class AuditError(RuntimeError):
    pass


def read_symvers(path: Path) -> dict[str, int]:
    exports = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) < 3:
            continue
        crc_text, symbol, owner = fields[:3]
        if owner != "vmlinux":
            continue
        try:
            crc = int(crc_text, 16) & 0xFFFFFFFF
        except ValueError as exc:
            raise AuditError(f"Invalid CRC on Module.symvers line {number}") from exc
        if symbol in exports and exports[symbol] != crc:
            raise AuditError(f"Conflicting vmlinux CRC for {symbol}")
        exports[symbol] = crc
    if not exports:
        raise AuditError("Module.symvers has no vmlinux exports")
    return exports


def read_versions(blob: bytes) -> dict[str, int]:
    if len(blob) < 64 or blob[:6] != b"\x7fELF\x02\x01":
        raise AuditError("Expected little-endian ELF64 module")
    if struct.unpack_from("<H", blob, 18)[0] != 183:
        raise AuditError("Expected AArch64 module")
    section_offset = struct.unpack_from("<Q", blob, 40)[0]
    entry_size, count, strings_index = struct.unpack_from("<HHH", blob, 58)
    if entry_size < 64 or count == 0 or strings_index >= count:
        raise AuditError("Invalid ELF section table")
    if section_offset + count * entry_size > len(blob):
        raise AuditError("Truncated ELF section table")

    def section(index: int) -> tuple[int, int, int]:
        offset = section_offset + index * entry_size
        name, section_type = struct.unpack_from("<II", blob, offset)
        data_offset, size = struct.unpack_from("<QQ", blob, offset + 24)
        if section_type != 8 and data_offset + size > len(blob):  # SHT_NOBITS has no file payload.
            raise AuditError("Truncated ELF section")
        return name, data_offset, size

    _, names_offset, names_size = section(strings_index)
    names = blob[names_offset:names_offset + names_size]
    for index in range(count):
        name_offset, data_offset, size = section(index)
        if name_offset >= len(names):
            raise AuditError("Invalid ELF section name")
        name = names[name_offset:].split(b"\0", 1)[0]
        if name != b"__versions":
            continue
        if size % 64:
            raise AuditError("Invalid __versions entry size")
        versions = {}
        data = blob[data_offset:data_offset + size]
        for pos in range(0, len(data), 64):
            crc = struct.unpack_from("<Q", data, pos)[0] & 0xFFFFFFFF
            symbol = data[pos + 8:pos + 64].split(b"\0", 1)[0].decode("ascii")
            if not symbol:
                raise AuditError("Empty __versions symbol")
            if symbol in versions and versions[symbol] != crc:
                raise AuditError(f"Conflicting module CRC for {symbol}")
            versions[symbol] = crc
        return versions
    raise AuditError("Module has no __versions section")


def vendor_modules(adb: str, vendor_dir: str):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", vendor_dir):
        raise AuditError("Unsafe vendor directory")
    listing = subprocess.run(
        [adb, "shell", "su", "-c", f"ls -1 {vendor_dir}/*.ko"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout.splitlines()
    paths = sorted(set(line.strip() for line in listing))
    if not paths:
        raise AuditError("No vendor modules found")
    for path in paths:
        if not re.fullmatch(re.escape(vendor_dir) + r"/[A-Za-z0-9_.-]+\.ko", path):
            raise AuditError("Unexpected module path in ADB listing")
        result = subprocess.run(
            [adb, "exec-out", "su", "-c", f"cat {path}"],
            capture_output=True, timeout=30, check=True,
        )
        yield Path(path).name, result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symvers", type=Path, help="Module.symvers from the candidate build")
    parser.add_argument("modules", nargs="*", type=Path, help="Locally supplied vendor .ko files")
    parser.add_argument("--adb", action="store_true", help="Read /vendor/lib/modules over ADB without saving files")
    parser.add_argument("--adb-path", default="adb", help="Path to adb executable")
    parser.add_argument("--vendor-dir", default="/vendor/lib/modules")
    args = parser.parse_args()
    try:
        exports = read_symvers(args.symvers)
        if args.adb:
            adb = shutil.which(args.adb_path) or args.adb_path
            modules = vendor_modules(adb, args.vendor_dir)
        elif args.modules:
            modules = ((p.name, p.read_bytes()) for p in args.modules)
        else:
            parser.error("Provide .ko files or --adb")
        mismatch = []
        compared = missing = module_count = 0
        for name, blob in modules:
            module_count += 1
            versions = read_versions(blob)
            for symbol, actual in versions.items():
                if symbol not in exports:
                    missing += 1  # May be exported by another vendor module.
                    continue
                compared += 1
                expected = exports[symbol]
                if actual != expected:
                    mismatch.append((name, symbol, actual, expected))
        print(f"Modules: {module_count}; comparable CRC references: {compared}; "
              f"mismatches: {len(mismatch)}; other-module/unknown references: {missing}")
        for name, symbol, actual, expected in mismatch[:30]:
            print(f"{name}: {symbol}: vendor=0x{actual:08x} kernel=0x{expected:08x}")
        if len(mismatch) > 30:
            print(f"... {len(mismatch) - 30} more mismatches")
        if compared == 0:
            print("Inconclusive: no symbols could be compared")
            return 3
        return 2 if mismatch else 0
    except (AuditError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"Audit failed: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
