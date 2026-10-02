"""Replace only the kernel in a device boot image with AOSP's boot tools."""
import argparse
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def repack_boot(base, image, output, tools):
    base, image, output, tools = map(lambda p: Path(p).resolve(), (base, image, output, tools))
    if output in (base, image):
        raise ValueError("Output must not overwrite the original boot image or kernel")
    with tempfile.TemporaryDirectory(prefix="gki-repack-") as temporary:
        args = subprocess.run([
            sys.executable, str(tools / "unpack_bootimg.py"), "--boot_img", str(base),
            "--out", temporary, "--format=mkbootimg",
        ], check=True, capture_output=True, text=True).stdout
        tokens = shlex.split(args)
        if "--kernel" not in tokens:
            raise RuntimeError("AOSP unpacker did not produce kernel arguments")
        tokens[tokens.index("--kernel") + 1] = str(image)
        subprocess.run([sys.executable, str(tools / "mkbootimg.py"), *tokens,
                        "--output", str(output)], check=True)
        # Check the actual payload and preserved ramdisk after packing.
        verify = Path(temporary) / "verify"
        subprocess.run([sys.executable, str(tools / "unpack_bootimg.py"),
                        "--boot_img", str(output), "--out", str(verify)],
                       check=True, capture_output=True)
        if (verify / "kernel").read_bytes() != image.read_bytes():
            raise RuntimeError("Repacked kernel differs from the compiled Image")
        ramdisk = Path(temporary) / "ramdisk"
        if ramdisk.exists() and (verify / "ramdisk").read_bytes() != ramdisk.read_bytes():
            raise RuntimeError("Repacking changed the device ramdisk")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-boot", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tools", required=True, help="AOSP tools/mkbootimg directory")
    args = parser.parse_args()
    repack_boot(args.base_boot, args.image, args.output, args.tools)
