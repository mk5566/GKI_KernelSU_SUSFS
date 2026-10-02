#!/usr/bin/env python3
"""Resolve SukiSU, integrate the one kernel stack, then compile."""
import argparse
import json
import logging
import os
from pathlib import Path

from config import BuildConfig, KSUChannel
from kernel_builder import KernelBuilder
from target import resolve_sukisu


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="android13-5.15.211 GKI-derived build")
    parser.add_argument("--sukisu-channel", choices=[x.value for x in KSUChannel], default="stable")
    parser.add_argument("--workspace", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--preflight-only", action="store_true",
                        help="Apply the source stack and generate/verify .config, then stop")
    parser.add_argument("--output-json")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = parse_args(argv)
    config = BuildConfig(args.sukisu_channel)
    config.sukisu_tag, config.sukisu_commit = resolve_sukisu(config.sukisu_channel)
    logging.info("GKI %s %s; SukiSU %s %s",
                 config.kernel_version, config.gki_commit,
                 config.sukisu_tag or "dev", config.sukisu_commit)
    builder = KernelBuilder(config, args.workspace)
    result = builder.build(preflight_only=args.preflight_only)
    if args.output_json:
        Path(args.output_json).write_text(json.dumps({
            "config": config.to_dict(), "success": result.success,
            "message": result.message, "artifacts": result.artifacts}, indent=2), encoding="utf-8")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
