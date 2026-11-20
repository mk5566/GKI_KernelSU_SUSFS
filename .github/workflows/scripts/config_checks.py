"""Independent verification of generated Kconfig; not defconfig generation.

Both the orchestrator and evidence publication use this exact policy. The AOSP
build script may legitimately adjust non-profile symbols after its defconfig
hook (for example TRIM_UNUSED_KSYMS), so the complete stage delta is recorded,
not blindly required to be empty.
"""
from pathlib import Path
from integrity import sha256
from kernel_profile import KernelProfile


def unquote_kconfig_string(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


class ConfigPolicy(KernelProfile):
    @staticmethod
    def parse(path: Path, what: str = "generated .config") -> dict:
        if not path.is_file():
            raise RuntimeError(f"{what} not found: {path}")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CONFIG_"):
                if "=" not in line:
                    raise RuntimeError(f"Malformed .config assignment: {line}")
                key, value = line.split("=", 1)
                if key in values:
                    raise RuntimeError(f"Final .config has duplicate assignment: {key}")
                values[key] = value
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                key = line[2:-11]
                if key in values:
                    raise RuntimeError(f"Final .config has duplicate assignment: {key}")
                values[key] = "n"
        return values

    @classmethod
    def verify(cls, path: Path) -> dict:
        values = cls.parse(path)
        # Every expectation below comes from one of the profile dicts, except
        # the zram settings (which live with the zram defconfig edits) and the
        # certified metadata/integrity settings guarded by the project rules.
        expected = {
            "CONFIG_ZRAM": "y", "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4KD": "y", "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y", "CONFIG_ZRAM_DEF_COMP_LZ4": "y",
            "CONFIG_ZRAM_DEF_COMP": '"lz4"', "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_LZ4_COMPRESS": "y", "CONFIG_LZ4_DECOMPRESS": "y",
            "CONFIG_MODVERSIONS": "y", "CONFIG_CFI_CLANG": "y",
            "CONFIG_LTO_CLANG_FULL": "y",
            "CONFIG_DEBUG_INFO": "y", "CONFIG_DEBUG_INFO_BTF": "y",
            "CONFIG_DEBUG_INFO_BTF_MODULES": "y",
            "CONFIG_DEBUG_INFO_DWARF4": "y", "CONFIG_SECURITY_SELINUX": "y",
            "CONFIG_MODULE_SIG_FORCE": "n",
            "CONFIG_LRU_GEN_STATS": "n", "CONFIG_LTO_CLANG_THIN": "n",
            "CONFIG_CFI_PERMISSIVE": "n",
        }
        # The root/logging/performance, network and runtime profiles are
        # asserted after Kconfig resolves dependencies, not from defconfig text.
        expected.update({key: value or "n" for key, value in cls.KERNEL_CONFIG_UPDATES.items()})
        expected.update(cls.NETWORK_CONFIG)
        expected.update(cls.RUNTIME_CONFIG)
        for symbol in ("BIC", "HTCP", "WESTWOOD", "VEGAS", "VENO", "HYBLA",
                       "ILLINOIS", "DCTCP", "CDG", "NV", "CUBIC"):
            expected[f"CONFIG_TCP_CONG_{symbol}"] = "n"
        for symbol in ("LZO", "LZORLE", "LZ4KD", "LZ4HC", "ZSTD", "DEFLATE", "842"):
            expected[f"CONFIG_ZRAM_DEF_COMP_{symbol}"] = "n"
        for key, want in expected.items():
            got = values.get(key, "n")
            if got != want:
                raise RuntimeError(f"Final .config mismatch: {key}={got}, expected {want}")
        cls._verify_runtime_config(values)
        cls._verify_susfs_config(values)
        return {
            "status": "passed", "sha256": sha256(path), "expected": expected,
            "actual": {key: values.get(key, "n") for key in expected},
            "cmdline": unquote_kconfig_string(values.get("CONFIG_CMDLINE", "")),
        }

    @classmethod
    def _verify_runtime_config(cls, values):
        cmdline = unquote_kconfig_string(values.get("CONFIG_CMDLINE", ""))
        tokens = cmdline.split()
        for token in cls.CMDLINE_TOKENS:
            prefix = token.split("=", 1)[0] + "="
            if tokens.count(token) != 1 or any(part.startswith(prefix) and part != token for part in tokens):
                raise RuntimeError(
                    f"Final .config command line must contain {token} once, got: {cmdline}")
        if any(part.startswith(("rcu_nocbs=", "rcutree.enable_rcu_lazy="))
               for part in tokens):
            raise RuntimeError(
                "Final .config mismatch: lazy RCU and callback offload stay "
                "at the certified default")

    @classmethod
    def _verify_susfs_config(cls, config_or_values):
        if isinstance(config_or_values, dict):
            values = config_or_values
        else:
            values = cls.parse(config_or_values, "compiled kernel .config")
        for symbol in ("CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_KRETPROBES",
                       "CONFIG_HAVE_SYSCALL_TRACEPOINTS", "CONFIG_KSU_SUSFS", "CONFIG_KSU_SUSFS_SUS_MOUNT"):
            if values.get(symbol) != "y":
                raise RuntimeError(f"Built-in SukiSU requires {symbol}=y in the compiled config")
        enabled = {key for key, val in values.items()
                   if key.startswith("CONFIG_KSU_SUSFS") and val in ("y", "m")}
        expected = {key for key, value in cls.KERNEL_CONFIG_UPDATES.items()
                    if key.startswith("CONFIG_KSU_SUSFS") and value == "y"}
        if enabled != expected:
            raise RuntimeError(f"SUSFS profile mismatch: expected {sorted(expected)}, got {sorted(enabled)}")


def config_delta(before: Path, after: Path) -> dict:
    left, right = ConfigPolicy.parse(before), ConfigPolicy.parse(after)
    return {key: {"precompile": left.get(key), "final": right.get(key)}
            for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)}
