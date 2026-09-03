#!/usr/bin/env python3
import argparse
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))

from config import BuildConfig, AndroidVersion, KernelVersion, ANDROID_KERNEL_MAP, KSUVersion
from kernel_builder import KernelBuilder, BuildResult

logging.basicConfig(
    level=logging.INFO,
    format='\033[92m[%(levelname)s]\033[0m %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

LOCKED_TARGET = {
    "android": "android13",
    "kernel": "5.15",
    "sub_level": "180",
    "os_patch_level": "2025-05",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKI Kernel Build System (android13-5.15.180)")

    parser.add_argument("--android", "-a", choices=[v.value for v in AndroidVersion], default=LOCKED_TARGET["android"])
    parser.add_argument("--kernel", "-k", choices=[v.value for v in KernelVersion], default=LOCKED_TARGET["kernel"])
    parser.add_argument("--sub-level", "-s", default=LOCKED_TARGET["sub_level"])
    parser.add_argument("--os-patch", default=LOCKED_TARGET["os_patch_level"])
    parser.add_argument("--ksu-version", choices=[v.value for v in KSUVersion], default=KSUVersion.STABLE.value)
    parser.add_argument("--ksu-commit", default=None)
    parser.add_argument("--susfs-commit", default=None)
    parser.add_argument("--zram", action="store_true", default=True, help="Enable ZRAM (LZ4KD)")
    parser.add_argument("--no-zram", action="store_false", dest="zram", help="Disable ZRAM")
    parser.add_argument("--bbr", action="store_true", default=True, help="Set BBR as default congestion control")
    parser.add_argument("--no-bbr", action="store_false", dest="bbr", help="Do not force BBR as default")
    parser.add_argument("--no-release", action="store_true", help="Do not create GitHub Release")
    parser.add_argument("--custom-version", dest="custom_version", default=None)
    parser.add_argument("--list-configs", action="store_true")
    parser.add_argument("--workspace", "-w", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def create_build_config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        android_version=args.android or LOCKED_TARGET["android"],
        kernel_version=args.kernel or LOCKED_TARGET["kernel"],
        sub_level=args.sub_level or LOCKED_TARGET["sub_level"],
        os_patch_level=args.os_patch or LOCKED_TARGET["os_patch_level"],
        kernelsu_version=args.ksu_version,
        kernelsu_commit=args.ksu_commit,
        susfs_commit=args.susfs_commit,
        use_zram=args.zram,
        set_default_bbr=args.bbr,
        make_release=not args.no_release,
        custom_version=args.custom_version,
    )


def list_configs():
    print("\n" + "=" * 60)
    print("Locked GKI target")
    print("=" * 60)
    print(
        f"  {LOCKED_TARGET['android']}-{LOCKED_TARGET['kernel']}."
        f"{LOCKED_TARGET['sub_level']}  (OS patch {LOCKED_TARGET['os_patch_level']})"
    )
    print("\nSupported combinations:")
    for android, kernels in ANDROID_KERNEL_MAP.items():
        print(f"  {android.value}: {', '.join(k.value for k in kernels)}")
    print("\n" + "=" * 60)
    print("KernelSU Version Options")
    print("=" * 60)
    for v in KSUVersion:
        print(f"  - {v.value}")


def _validate_vendor_patches(config: BuildConfig) -> list:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    patch_dir = repo_root / "patches" / f"{config.kernel_version}.{config.sub_level}"
    missing = []
    if not patch_dir.exists():
        return [f"patch directory missing: {patch_dir}"]
    order_file = patch_dir / "APPLY_ORDER.txt"
    names = []
    if order_file.exists():
        for raw in order_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line[1:].strip() if line.startswith("!") else line)
    else:
        names = [p.name for p in patch_dir.glob("*.patch")]
    for name in names:
        if not (patch_dir / name).exists():
            missing.append(str(patch_dir / name))
    return missing


def build_single(config: BuildConfig, workspace: str, dry_run: bool = False) -> BuildResult:
    if dry_run:
        logger.info(f"[DRY RUN] Validating config: {config.config_name}")
        missing = _validate_vendor_patches(config)
        if missing:
            logger.error("Missing vendor patches:")
            for path in missing:
                logger.error(f"  - {path}")
            return BuildResult(success=False, config=config, message="Missing vendor patches")
        logger.info("[DRY RUN] Vendor patches present")
        return BuildResult(success=True, config=config, message="Configuration validation passed")

    builder = KernelBuilder(config, workspace)
    return builder.build()


def print_summary(results: list, output_json: str = None):
    total = len(results)
    success = sum(1 for r in results if r.success)

    print("\n" + "=" * 60)
    print("Build Summary")
    print("=" * 60)
    print(f"Total: {total}")
    print(f"Success: \033[92m{success}\033[0m")
    print(f"Failed: \033[91m{total - success}\033[0m")

    if success > 0:
        avg_time = sum(r.build_time or 0 for r in results if r.success) / success
        print(f"Average Build Time: {avg_time:.2f} s")

    failed = total - success
    if failed > 0:
        print("\nFailed configurations:")
        for r in results:
            if not r.success:
                print(f"  - {r.config.config_name}: {r.message}")
    print("=" * 60)

    if output_json:
        json_data = {
            "timestamp": datetime.now().isoformat(),
            "total": total,
            "success": success,
            "failed": failed,
            "results": [{"config": r.config.to_dict(), "success": r.success, "message": r.message,
                       "artifacts": r.artifacts, "build_time": r.build_time} for r in results]
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Results saved to: {output_json}")


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_configs:
        list_configs()
        return 0

    workspace = args.workspace
    logger.info(f"Workspace: {workspace}")
    os.makedirs(workspace, exist_ok=True)

    try:
        config = create_build_config(args)
        result = build_single(config, workspace, args.dry_run)
        results = [result]
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        return 1

    print_summary(results, args.output_json)

    if all(r.success for r in results):
        return 0
    if any(r.success for r in results):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
