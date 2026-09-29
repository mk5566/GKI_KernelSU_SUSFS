#!/usr/bin/env python3
"""Resolve the run once, integrate the complete stack, then compile."""
import argparse
import json
import logging
import os
from pathlib import Path

from config import BuildConfig, KSUChannel, BBRVersion
from kernel_builder import KernelBuilder
from target import resolve_gki, resolve_sukisu


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Android 13 Linux 5.15 GKI-derived build")
    parser.add_argument("--sukisu-channel", choices=[x.value for x in KSUChannel], default="stable")
    parser.add_argument("--bbr-version", choices=[x.value for x in BBRVersion], default="v3")
    tweaks = parser.add_mutually_exclusive_group()
    tweaks.add_argument("--apply-tweaks", dest="apply_tweaks", action="store_true", default=True)
    tweaks.add_argument("--no-tweaks", dest="apply_tweaks", action="store_false")
    parser.add_argument("--workspace", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--preflight-only", action="store_true",
                        help="Apply the same complete source stack and generate/verify .config, then stop")
    parser.add_argument("--output-json")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = parse_args(argv)
    config = BuildConfig(args.sukisu_channel, args.bbr_version, args.apply_tweaks)
    target = resolve_gki()
    config.gki_tag = target.tag
    config.gki_commit = target.commit
    config.kernel_version = target.kernel_version
    config.manifest_branch = target.manifest_branch
    config.sukisu_tag, config.sukisu_commit = resolve_sukisu(config.sukisu_channel)
    logging.info("GKI %s %s; SukiSU %s %s; BBR%s; tweaks=%s",
                 config.gki_tag, config.gki_commit, config.sukisu_tag or "dev",
                 config.sukisu_commit, config.bbr_version, config.apply_tweaks)
    builder = KernelBuilder(config, args.workspace)
    result = builder.build(preflight_only=args.preflight_only)
    if args.output_json:
        Path(args.output_json).write_text(json.dumps({
            "config": config.to_dict(), "success": result.success,
            "message": result.message, "artifacts": result.artifacts}, indent=2), encoding="utf-8")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
