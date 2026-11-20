"""Read Android boot v0-v4 kernel payloads without executing packaging tools.

This checks payload identity, not AVB, signatures, device compatibility or bootability.
Layout source: AOSP system/tools/mkbootimg/include/bootimg/bootimg.h.
"""
import hashlib
import struct
from pathlib import Path

from integrity import sha256


def verify_boot_image(boot: Path, image: Path) -> dict[str, int | str]:
    with boot.open("rb") as stream:
        header = stream.read(44)
        if len(header) != 44 or header[:8] != b"ANDROID!":
            raise RuntimeError("Invalid Android boot image header")
        kernel_size = struct.unpack_from("<I", header, 8)[0]
        version = struct.unpack_from("<I", header, 40)[0]
        if version in (3, 4):
            page_size = 4096
            header_size = struct.unpack_from("<I", header, 20)[0]
            if header_size != {3: 1580, 4: 1584}[version]:
                raise RuntimeError("Invalid Android boot header size")
        elif version in (0, 1, 2):
            page_size = struct.unpack_from("<I", header, 36)[0]
            if page_size < 2048 or page_size > 65536 or page_size & (page_size - 1):
                raise RuntimeError("Invalid legacy boot page size")
        else:
            raise RuntimeError(f"Unsupported Android boot header version: {version}")
        if not kernel_size or kernel_size != image.stat().st_size:
            raise RuntimeError("Boot kernel size differs from compiled Image")
        if boot.stat().st_size < page_size + kernel_size:
            raise RuntimeError("Truncated Android boot kernel payload")
        digest = hashlib.sha256()
        stream.seek(page_size)
        remaining = kernel_size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Truncated Android boot kernel payload")
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != sha256(image):
        raise RuntimeError("Boot kernel payload differs from compiled Image")
    return {"header_version": version, "kernel_size": kernel_size,
            "kernel_sha256": digest.hexdigest()}
