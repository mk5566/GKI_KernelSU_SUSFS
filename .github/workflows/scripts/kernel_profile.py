"""Reviewed kernel selections. Changes here require kernel/KMI review."""


class KernelProfile:
    KERNEL_CONFIG_UPDATES = {
        "CONFIG_KSU": "y",
        "CONFIG_KPROBES": "y",
        "CONFIG_KRETPROBES": "y",
        "CONFIG_HAVE_SYSCALL_TRACEPOINTS": "y",
        "CONFIG_KSU_DEBUG": "n",
        # Mount-only SUSFS on the upstream `builtin` branch, which carries the
        # KernelSU-side integration natively. Only mount hiding is enabled;
        # every other declared SUSFS sub-option is forced off in
        # configure_kernel (see the declared-symbol scan below).
        "CONFIG_KSU_SUSFS": "y",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
        "CONFIG_KPM": "n",
        "CONFIG_TMPFS_XATTR": "y",
        "CONFIG_TMPFS_POSIX_ACL": "y",
        "CONFIG_IP_NF_TARGET_TTL": "y",
        "CONFIG_IP6_NF_TARGET_HL": "y",
        "CONFIG_IP6_NF_MATCH_HL": "y",
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "y",
        "CONFIG_CC_OPTIMIZE_FOR_SIZE": None,
        # Preserve certified DWARF/BTF defaults: BTF module metadata changes
        # struct module and its transitive vendor symbol CRCs. It is not logging.
    }

    # BBRv3 is the default. Stock BBRv1 stays built in as the fallback.
    # Keep the original 104-byte private area. This is necessary, not proof of
    # complete binary KMI compatibility; hidden type changes need ABI review.
    NETWORK_CONFIG = {
        "CONFIG_TCP_CONG_ADVANCED": "y",
        "CONFIG_TCP_CONG_BBR": "y",
        "CONFIG_TCP_CONG_BBR3": "y",
        "CONFIG_DEFAULT_BBR3": "y",
        "CONFIG_DEFAULT_BBR": "n",
        "CONFIG_DEFAULT_CUBIC": "n",
        "CONFIG_DEFAULT_TCP_CONG": '"bbr3"',
        "CONFIG_NET_SCH_FQ": "y",
    }

    # android13-5.15 already builds MGLRU, lazy RCU, menu, and TEO. It has
    # no cpuidle default-governor choice, and menu is rated above TEO, so
    # the built-in command line selects TEO. Bootloader arguments stay
    # appended through CONFIG_CMDLINE_EXTEND.
    #
    # Lazy RCU stays at the certified default. Turning it on requires
    # rcu_nocbs=all, which offloads every CPU, including the boot CPU, while
    # the offload threads are not created until early_initcall. That pair
    # stalled fastboot boot and the bootloader returned to the flashed kernel.
    RUNTIME_CONFIG = {
        "CONFIG_LRU_GEN": "y",
        "CONFIG_LRU_GEN_ENABLED": "y",
        "CONFIG_RCU_EXPERT": "y",
        "CONFIG_RCU_NOCB_CPU": "y",
        "CONFIG_RCU_LAZY": "y",
        "CONFIG_RCU_LAZY_DEFAULT_OFF": "y",
        "CONFIG_CPU_IDLE": "y",
        "CONFIG_CPU_IDLE_GOV_MENU": "y",
        "CONFIG_CPU_IDLE_GOV_TEO": "y",
        "CONFIG_CMDLINE_EXTEND": "y",
        "CONFIG_CMDLINE_FROM_BOOTLOADER": "n",
        "CONFIG_CMDLINE_FORCE": "n",
    }
    CMDLINE_TOKENS = (
        "cpuidle.governor=teo",
    )
    # Human-readable names for the settings this profile is about. The final
    # .config check is the authoritative assertion; this only makes the log
    # readable, so it must not become a second place that names a symbol.
    CONFIG_SUMMARY_LABELS = {
        "CONFIG_KSU": "KernelSU",
        "CONFIG_KSU_SUSFS": "SUSFS",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "SUSFS mount hiding",
        "CONFIG_KSU_SUSFS_TRY_UMOUNT": "SUSFS try_umount",
        "CONFIG_KSU_SUSFS_SUS_SU": "SUSFS sus_su",
        "CONFIG_TCP_CONG_BBR3": "BBRv3",
        "CONFIG_TCP_CONG_BBR": "BBRv1",
        "CONFIG_DEFAULT_TCP_CONG": "Default TCP cong",
        "CONFIG_ZRAM": "ZRAM",
        "CONFIG_ZRAM_DEF_COMP_LZ4": "ZRAM lz4 default",
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "Optimize for performance",
        "CONFIG_DEBUG_INFO_BTF": "GKI BTF metadata",
        "CONFIG_LRU_GEN_ENABLED": "Multi-gen LRU",
        "CONFIG_RCU_LAZY": "Lazy RCU",
        "CONFIG_RCU_LAZY_DEFAULT_OFF": "Lazy RCU default off",
        "CONFIG_CPU_IDLE_GOV_TEO": "TEO cpuidle governor",
        "CONFIG_CPU_IDLE_GOV_MENU": "Menu cpuidle governor",
    }
