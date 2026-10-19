"""Verify the built kernel against the selected certified GKI symbol CRCs."""
import urllib.request


def artifact_url(build_id, name):
    if not build_id.isdigit() or name not in ("vmlinux.symvers", f"manifest_{build_id}.xml"):
        raise ValueError("Invalid certified GKI artifact")
    # Use AOSP's public download_from_ci route. The internal v3 /url API
    # returns 501 for these public artifacts, including the CRC reference.
    # Keep both consumers on the same exact certified build and target.
    return (f"https://ci.android.com/builds/submitted/{build_id}/"
            f"kernel_aarch64/latest/raw/{name}")


def read_symvers(text):
    result = {}
    for line in text.splitlines():
        columns = line.split()
        if len(columns) >= 4 and columns[2] == "vmlinux":
            result[columns[1]] = columns[0]
    if not result:
        raise RuntimeError("Empty GKI symbol version table")
    return result


def compare_symvers(reference, built):
    """Additional built-in exports are permitted; frozen exports must match."""
    problems = []
    for symbol, crc in reference.items():
        if symbol not in built:
            problems.append(f"{symbol}: missing frozen export")
        elif built[symbol] != crc:
            problems.append(f"{symbol}: CRC {built[symbol]} != GKI {crc}")
    if problems:
        raise RuntimeError(f"GKI ABI mismatch ({len(problems)} symbols):\n" + "\n".join(problems[:40]))
    return len(reference)


def verify_gki_abi(build_id, built_path, reference_path):
    with urllib.request.urlopen(artifact_url(build_id, "vmlinux.symvers"), timeout=60) as response:
        reference = response.read().decode("utf-8")
    reference_path.write_text(reference, encoding="utf-8")
    return compare_symvers(read_symvers(reference), read_symvers(built_path.read_text(encoding="utf-8")))
