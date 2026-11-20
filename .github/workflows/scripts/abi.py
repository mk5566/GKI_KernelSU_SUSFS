"""Verify the built kernel against the selected certified GKI symbol CRCs."""
import re
from dataclasses import dataclass
import urllib.request


def artifact_url(build_id, name):
    if not build_id.isdigit() or name not in ("vmlinux.symvers", f"manifest_{build_id}.xml"):
        raise ValueError("Invalid certified GKI artifact")
    # Use AOSP's public download_from_ci route. The internal v3 /url API
    # returns 501 for these public artifacts, including the CRC reference.
    # Keep both consumers on the same exact certified build and target.
    return (f"https://ci.android.com/builds/submitted/{build_id}/"
            f"kernel_aarch64/latest/raw/{name}")


@dataclass(frozen=True)
class Export:
    crc: str
    export_type: str
    namespace: str


def read_exports(text: str) -> dict[str, Export]:
    """Parse modpost's four/five-column format without dropping bad records.

    Export namespaces are optional. Module exports are validated but excluded
    from the vmlinux comparison. Duplicate names are never silently overwritten.
    """
    result: dict[str, Export] = {}
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        columns = line.split()
        if not columns:
            continue
        if (len(columns) not in (4, 5)
                or not re.fullmatch(r"0[xX][0-9a-fA-F]{1,8}", columns[0])
                or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", columns[1])
                or not re.fullmatch(r"EXPORT_(?:UNUSED_)?SYMBOL(?:_GPL(?:_FUTURE)?)?", columns[3])):
            raise RuntimeError(f"Malformed GKI symbol version row {number}: {line[:180]}")
        crc, symbol, provider = columns[:3]
        if symbol in seen:
            raise RuntimeError(f"Duplicate GKI symbol version row {number}: {symbol}")
        seen.add(symbol)
        if provider == "vmlinux":
            result[symbol] = Export(f"0x{int(crc, 16):08x}", columns[3], columns[4] if len(columns) == 5 else "")
    if not result:
        raise RuntimeError("Empty GKI symbol version table")
    return result



def read_symvers(text: str) -> dict[str, str]:
    """Compatibility API for callers that need just CRCs, not export policy."""
    return {symbol: entry.crc for symbol, entry in read_exports(text).items()}


def compare_exports(reference: dict[str, Export], built: dict[str, Export]) -> int:
    matched = compare_symvers({key: entry.crc for key, entry in reference.items()},
                              {key: entry.crc for key, entry in built.items()})
    problems = [f"{key}: export type/namespace {(built[key].export_type, built[key].namespace)!r} "
                f"!= certified {(entry.export_type, entry.namespace)!r}"
                for key, entry in reference.items()
                if (built[key].export_type, built[key].namespace) != (entry.export_type, entry.namespace)]
    if problems:
        raise RuntimeError(f"GKI export metadata mismatch ({len(problems)} symbols):\n" + "\n".join(problems[:40]))
    return matched


def compare_symvers(reference, built):
    """Additional built-in exports are permitted; frozen exports must match."""
    if not reference:
        raise RuntimeError("Empty GKI symbol version table")
    problems = []
    for symbol, crc in reference.items():
        if symbol not in built:
            problems.append(f"{symbol}: missing frozen export")
        elif int(built[symbol], 16) != int(crc, 16):
            problems.append(f"{symbol}: CRC {built[symbol]} != GKI {crc}")
    if problems:
        raise RuntimeError(f"GKI ABI mismatch ({len(problems)} symbols):\n" + "\n".join(problems[:40]))
    return len(reference)


def verify_gki_abi(build_id, built_path, reference_path):
    with urllib.request.urlopen(artifact_url(build_id, "vmlinux.symvers"), timeout=60) as response:
        reference = response.read().decode("utf-8")
    reference_path.write_text(reference, encoding="utf-8")
    return compare_exports(read_exports(reference), read_exports(built_path.read_text(encoding="utf-8")))
