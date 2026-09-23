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

from config import (
    BuildConfig, AndroidVersion, KernelVersion, ANDROID_KERNEL_MAP, KSUVersion,
)
from kernel_builder import KernelBuilder, BuildResult
from target import TargetSelection, resolve_latest_target
from patch_utils import parse_optional_patches, read_patch_order

logging.basicConfig(
    level=logging.INFO,
    format='\033[92m[%(levelname)s]\033[0m %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKI Kernel Build System (android13-5.15)")

    parser.add_argument("--android", "-a", choices=[v.value for v in AndroidVersion], default=AndroidVersion.ANDROID13.value)
    parser.add_argument("--kernel", "-k", choices=[v.value for v in KernelVersion], default=KernelVersion.KERNEL_5_15.value)
    parser.add_argument("--sub-level", "-s", default="auto")
    parser.add_argument("--os-patch", default="auto")
    parser.add_argument("--target-file", help="Target selection saved by the CI resolve step")
    parser.add_argument("--ksu-version", choices=[v.value for v in KSUVersion], default=KSUVersion.DEV.value)
    parser.add_argument("--ksu-commit", default=None)
    parser.add_argument("--susfs-commit", default=None)
    parser.add_argument("--zram", action="store_true", default=False, help="Request built-in ZSTD instead of the ishtar LZ4KD default")
    parser.add_argument("--no-zram", action="store_false", dest="zram", help="Use the ishtar built-in LZ4KD default")
    parser.add_argument("--bbr", action="store_true", default=False, help="Select upstream BBRv1 as default")
    parser.add_argument("--no-bbr", action="store_false", dest="bbr", help="Preserve upstream TCP defaults")
    parser.add_argument("--optional-patches", default="", help="Comma-separated aliases from patches/5.15/APPLY_ORDER.txt")
    parser.add_argument("--no-release", action="store_true", help="Do not create GitHub Release")
    parser.add_argument("--custom-version", dest="custom_version", default=None)
    parser.add_argument("--list-configs", action="store_true")
    parser.add_argument("--workspace", "-w", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def create_build_config(args: argparse.Namespace, selection: TargetSelection | None = None) -> BuildConfig:
    if selection is None:
        latest_sublevel, latest_patch = resolve_latest_target()
    else:
        latest_sublevel, latest_patch = selection.sublevel, selection.month
    if args.sub_level not in ("auto", latest_sublevel):
        raise ValueError("--sub-level disagrees with the latest GKI branch")
    if args.os_patch not in (None, "", "auto", latest_patch):
        raise ValueError("--os-patch disagrees with the latest GKI branch")
    sub_level, os_patch = latest_sublevel, latest_patch
    return BuildConfig(
        android_version=args.android,
        kernel_version=args.kernel,
        sub_level=sub_level,
        os_patch_level=os_patch,
        kernelsu_version=args.ksu_version,
        kernelsu_commit=args.ksu_commit,
        susfs_commit=args.susfs_commit,
        use_zram=args.zram,
        set_default_bbr=args.bbr,
        optional_patches=parse_optional_patches(args.optional_patches),
        make_release=not args.no_release,
        custom_version=args.custom_version,
    )


def list_configs():
    print("\n" + "=" * 60)
    print("Default GKI target: latest published android13-5.15 monthly branch")
    print("=" * 60)
    print("  android13-5.15; live sublevel and OS patch are resolved when a build starts")
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
    patch_dir = repo_root / "patches" / config.kernel_version
    missing = []
    for name in ("0001-sukisu-main-uapi4-mount-support.patch",
                 "0002-common-susfs-reboot-dispatch.patch"):
        integration_patch = repo_root / "patches/susfs" / name
        if not integration_patch.is_file():
            missing.append(str(integration_patch))
    context_patch = repo_root / "patches/susfs/5.15-context.patch"
    if not context_patch.is_file():
        missing.append(str(context_patch))
    try:
        read_patch_order(patch_dir, config.optional_patches)
    except (OSError, ValueError) as error:
        missing.append(str(error))
    return missing


def build_single(config: BuildConfig, workspace: str, dry_run: bool = False,
                 expected_common_revision: str | None = None) -> BuildResult:
    if dry_run:
        logger.info(f"[DRY RUN] Validating config: {config.config_name}")
        errors = _validate_vendor_patches(config)
        if errors:
            logger.error("Patch manifest or selection error:")
            for error in errors:
                logger.error(f"  - {error}")
            return BuildResult(success=False, config=config,
                               message="Patch manifest or selection validation failed")
        logger.info("[DRY RUN] Manifest/files valid; kernel applicability NOT tested")
        return BuildResult(success=True, config=config, message="Manifest validation passed; no kernel checkout tested")

    builder = KernelBuilder(config, workspace, expected_common_revision=expected_common_revision)
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
        selection = TargetSelection.from_file(args.target_file) if args.target_file else None
        config = create_build_config(args, selection)
        result = build_single(config, workspace, args.dry_run,
                              selection.common_revision if selection else None)
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
