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

DEFAULT_BUILD_MATRIX = {
    "android13-5.15": [
        {"sub_level": "180", "os_patch_level": "2025-05"},
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKI Kernel Build System")

    parser.add_argument("--android", "-a", choices=[v.value for v in AndroidVersion], default="android13")
    parser.add_argument("--kernel", "-k", choices=[v.value for v in KernelVersion], default="5.15")
    parser.add_argument("--sub-level", "-s", default="180")
    parser.add_argument("--os-patch", default="2025-05")
    parser.add_argument("--ksu-version", choices=[v.value for v in KSUVersion], default=KSUVersion.STABLE.value)
    parser.add_argument("--ksu-commit", default=None)
    parser.add_argument("--susfs-commit", default=None)
    parser.add_argument("--zram", action="store_true", default=True, help="Enable ZRAM (LZ4KD)")
    parser.add_argument("--no-zram", action="store_false", dest="zram", help="Disable ZRAM")
    parser.add_argument("--bbr", action="store_true", default=True, help="Set BBR as default congestion control")
    parser.add_argument("--no-bbr", action="store_false", dest="bbr", help="Do not force BBR as default")
    parser.add_argument("--no-release", action="store_true", help="Do not create GitHub Release")
    parser.add_argument("--custom-version", dest="custom_version", default=None)
    parser.add_argument("--matrix", "-m")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list-configs", action="store_true")
    parser.add_argument("--list-matrix", action="store_true")
    parser.add_argument("--workspace", "-w", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def create_build_config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        android_version=args.android or "android13",
        kernel_version=args.kernel or "5.15",
        sub_level=args.sub_level or "180",
        os_patch_level=args.os_patch or "2025-05",
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
    print("Supported Android / Kernel Combinations")
    print("=" * 60)
    for android, kernels in ANDROID_KERNEL_MAP.items():
        print(f"\n{android.value}:")
        for kernel in kernels:
            configs = DEFAULT_BUILD_MATRIX.get(f"{android.value}-{kernel.value}", [])
            print(f"  - {kernel.value}: {', '.join(c['sub_level'] for c in configs) or 'N/A'}")
    print("\n" + "=" * 60)
    print("KernelSU Version Options")
    print("=" * 60)
    for v in KSUVersion:
        print(f"  - {v.value}")


def list_matrix():
    print("\n" + "=" * 60)
    print("Predefined Build Matrix")
    print("=" * 60)
    for combo, configs in sorted(DEFAULT_BUILD_MATRIX.items()):
        print(f"\n{combo}:")
        for cfg in configs:
            rev = f" (rev: {cfg.get('revision', 'N/A')})" if cfg.get('revision') else ""
            print(f"  - {cfg['sub_level']:>4} | {cfg['os_patch_level']}{rev}")


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


def build_matrix(matrix_key: str, args: argparse.Namespace, workspace: str) -> list:
    logger.info(f"\n{'=' * 60}\nStarting matrix build: {matrix_key}\n{'=' * 60}\n")

    configs_data = DEFAULT_BUILD_MATRIX.get(matrix_key, [])
    if not configs_data:
        logger.error(f"Unknown matrix: {matrix_key}")
        return []

    results = []
    for cfg_data in configs_data:
        try:
            config = BuildConfig(
                android_version=matrix_key.split("-")[0],
                kernel_version=matrix_key.split("-")[1],
                sub_level=cfg_data["sub_level"],
                os_patch_level=cfg_data["os_patch_level"],
                kernelsu_version=args.ksu_version,
                kernelsu_commit=args.ksu_commit,
                use_zram=args.zram,
                set_default_bbr=args.bbr,
                make_release=not args.no_release,
                custom_version=args.custom_version,
            )

            logger.info(f"\n{'=' * 60}\nBuilding config: {config.config_name}\n{'=' * 60}")
            result = build_single(config, workspace, args.dry_run)
            results.append(result)

            if result.success:
                logger.info(f"✓ {config.config_name} build succeeded")
            else:
                logger.error(f"✗ {config.config_name} build failed: {result.message}")
        except Exception as e:
            logger.error(f"Config error for {cfg_data}: {e}")
            continue

    return results


def build_all(args: argparse.Namespace, workspace: str) -> list:
    all_results = []
    for matrix_key in sorted(DEFAULT_BUILD_MATRIX.keys()):
        results = build_matrix(matrix_key, args, workspace)
        all_results.extend(results)
    return all_results


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

    if args.list_matrix:
        list_matrix()
        return 0

    if not args.all and not args.matrix and not args.android:
        logger.error("Please specify --all, --matrix or --android")
        return 1

    workspace = args.workspace
    logger.info(f"Workspace: {workspace}")
    os.makedirs(workspace, exist_ok=True)

    results = []

    if args.all:
        results = build_all(args, workspace)
    elif args.matrix:
        results = build_matrix(args.matrix, args, workspace)
    else:
        try:
            config = create_build_config(args)
            result = build_single(config, workspace, args.dry_run)
            results.append(result)
        except Exception as e:
            logger.error(f"Configuration error: {e}")
            return 1

    if results:
        print_summary(results, args.output_json)

    if results and all(r.success for r in results):
        return 0
    elif results and any(r.success for r in results):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
