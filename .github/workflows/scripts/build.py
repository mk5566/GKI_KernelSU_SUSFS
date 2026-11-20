#!/usr/bin/env python3
"""Resolve the latest GKI stable and SukiSU, then compile one kernel."""
import argparse
import logging
import os
import re
from pathlib import Path

from config import BuildConfig, KSUChannel, REPO_TOOL_CONFIG
from kernel_builder import KernelBuilder
from source_lock import write_sources, load_sources
from integrity import atomic_json
from upstream_policy import make_upstream_status
from target import (resolve_gki, resolve_sukisu, resolve_susfs, resolve_sukisu_patch,
                    resolve_anykernel, resolve_branch, check_reviewed_upstreams)


def exact_commit(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("Commit must be a full lowercase 40-digit SHA")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="latest android13-5.15 GKI stable")
    parser.add_argument("--sukisu-channel", choices=[x.value for x in KSUChannel], default="stable")
    parser.add_argument("--sukisu-commit", type=exact_commit,
                        help="Build the exact SukiSU commit already checked by CI")
    parser.add_argument("--susfs-commit", type=exact_commit, help="Use an exact SUSFS commit")
    parser.add_argument("--sukisu-patch-commit", type=exact_commit, help="Use an exact LZ4KD helper commit")
    parser.add_argument("--anykernel-commit", type=exact_commit, help="Use an exact AnyKernel3 commit")
    parser.add_argument("--workspace", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resolve-only", action="store_true",
                        help="Resolve current dependencies and save workspace/sources.json, then stop")
    parser.add_argument("--sources", help="Build using the sources.json already resolved and tested in this run")
    parser.add_argument("--strict-upstreams", action="store_true",
                        help="Compatibility alias: upstream freshness checks are always fatal during resolution")
    modes.add_argument("--preflight-only", action="store_true",
                        help="Apply the source stack and generate/verify .config, then stop")
    parser.add_argument("--output-json")
    parser.add_argument("--base-boot", help="Repack a known booting device boot image, preserving its ramdisk and header")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = parse_args(argv)
    config = BuildConfig(args.sukisu_channel)
    config.base_boot = str(Path(args.base_boot).resolve()) if args.base_boot else ""
    if args.sources:
        if args.strict_upstreams:
            raise RuntimeError("--strict-upstreams applies during resolution, not to an existing --sources lock")
        if any((args.sukisu_commit, args.susfs_commit, args.sukisu_patch_commit, args.anykernel_commit)):
            raise RuntimeError("Do not override an already resolved source record")
        config = load_sources(args.sources)
        config.base_boot = str(Path(args.base_boot).resolve()) if args.base_boot else ""
    else:
        reviewed = check_reviewed_upstreams(strict=True)
        gki = resolve_gki()
        config.gki_tag = gki.tag
        config.gki_commit = gki.commit
        config.kernel_version = gki.kernel_version
        config.manifest_branch = gki.manifest_branch
        config.manifest_commit = gki.manifest_commit
        config.official_build_id = gki.official_build_id
        config.source_projects = gki.source_projects
        if args.sukisu_commit:
            config.sukisu_commit = args.sukisu_commit
        else:
            config.sukisu_tag, config.sukisu_commit = resolve_sukisu(config.sukisu_channel)
        # Resolve each helper once; checkout and evidence use these immutable SHAs.
        config.susfs_commit = args.susfs_commit or resolve_susfs()
        config.sukisu_patch_commit = args.sukisu_patch_commit or resolve_sukisu_patch()
        config.anykernel_commit = args.anykernel_commit or resolve_anykernel()
        config.repo_commit = resolve_branch(REPO_TOOL_CONFIG)
        explicit = [name for name in ("sukisu", "susfs", "sukisu_patch", "anykernel")
                    if getattr(args, name + "_commit")]
        config.upstream_status = make_upstream_status(config, reviewed, explicit)
    if args.resolve_only:
        config.validate()
        workspace = Path(args.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        logging.info("Resolved sources: %s", write_sources(config, workspace))
        return 0
    logging.info("GKI %s %s %s; manifest %s; SukiSU %s %s; SUSFS %s; SukiSU_patch %s; AnyKernel3 %s",
                 config.kernel_version, config.gki_tag, config.gki_commit,
                 config.manifest_branch, config.sukisu_tag or "exact source commit", config.sukisu_commit,
                 config.susfs_commit, config.sukisu_patch_commit, config.anykernel_commit)
    builder = KernelBuilder(config, args.workspace)
    result = builder.build(preflight_only=args.preflight_only)
    if args.output_json:
        atomic_json(Path(args.output_json), {
            "config": config.to_dict(), "success": result.success,
            "message": result.message, "artifacts": result.artifacts,
            "build_time_seconds": result.build_time})
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
