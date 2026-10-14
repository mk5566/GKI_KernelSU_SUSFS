"""Regression for the upstream layout that broke the UAPI-4-era patch."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig, REPO_ROOT
from kernel_builder import KernelBuilder


MOUNT_PATCH = REPO_ROOT / "patches/susfs/0001-sukisu-main-mount-support.patch"
QUIET_PATCH = REPO_ROOT / "patches/susfs/0003-sukisu-quiet-production.patch"


class UpstreamPatchTests(unittest.TestCase):
    def test_module_blacklist_and_forced_features_survive_mount_integration(self):
        # Unmodified source excerpts from upstream 0bca5d15, including the
        # additions that made the previous patch's context reject both files.
        kbuild = """kernelsu-objs := core/init.o

kernelsu-objs += feature/kernel_umount.o
kernelsu-objs += feature/module_blacklist.o
kernelsu-objs += feature/sulog.o
kernelsu-objs += feature/sucompat.o
"""
        boot = """#include "feature/selinux_hide.h"
#include <linux/err.h>
#include <linux/fs.h>
#include <linux/namei.h>
#include <linux/printk.h>

#include "policy/allowlist.h"
#include "policy/feature.h"
#include "klog.h" // IWYU pragma: keep
#include "runtime/ksud_boot.h"
#include "runtime/ksud.h"
#include "manager/manager_observer.h"
#include "manager/throne_tracker.h"

bool ksu_module_mounted __read_mostly = false;
bool ksu_boot_completed __read_mostly = false;

void on_post_fs_data(void)
{
    static bool done = false;

    if (done) {
        pr_info("on_post_fs_data already done\\n");
        return;
    }

    done = true;
    pr_info("on_post_fs_data!\\n");

    ksu_load_allow_list();
    ksu_observer_init();
    // Sanity check for safe mode only needs early-boot input samples.
    ksu_stop_input_hook_runtime();
    ksu_selinux_hide_handle_post_fs_data();
    // retry forced features which failed at second stage
    ksu_feature_apply_forced();
}

extern void ext4_unregister_sysfs(struct super_block *sb);
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "kernel/runtime").mkdir(parents=True)
            (root / "kernel/Kbuild").write_text(kbuild, encoding="utf-8")
            (root / "kernel/runtime/boot_event.c").write_text(boot, encoding="utf-8")
            sections = MOUNT_PATCH.read_text(encoding="utf-8").split("diff --git ")
            selected = "".join("diff --git " + section for section in sections[1:]
                               if section.startswith(("a/kernel/Kbuild ",
                                                      "a/kernel/runtime/boot_event.c ")))
            patch_file = root / "layout.patch"
            patch_file.write_text(selected, encoding="utf-8")
            builder = KernelBuilder(BuildConfig(), temp)
            builder.shell.cwd = str(root)
            builder._apply_patch_file(patch_file)
            result = (root / "kernel/Kbuild").read_text(encoding="utf-8")
            self.assertIn("kernelsu-$(CONFIG_KSU_SUSFS) += feature/susfs.o", result)
            self.assertIn("kernelsu-objs += feature/module_blacklist.o", result)
            result = (root / "kernel/runtime/boot_event.c").read_text(encoding="utf-8")
            self.assertIn('#include "policy/feature.h"', result)
            self.assertIn("#include <linux/susfs.h>", result)
            self.assertIn("susfs_start_sdcard_monitor_fn();", result)
            self.assertLess(result.index("ksu_feature_apply_forced();"),
                            result.index("susfs_start_sdcard_monitor_fn();"))

    @unittest.skipUnless(os.environ.get("SUKISU_TEST_SOURCE"),
                         "set SUKISU_TEST_SOURCE to check the complete upstream source")
    def test_complete_patch_stack_applies_to_upstream_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            checkout = builder.work_dir / "KernelSU"
            # Clone HEAD so an existing source's working changes cannot mask
            # incompatibilities. No network or mutations of that source.
            subprocess.run(["git", "clone", "--shared", "--quiet",
                            os.environ["SUKISU_TEST_SOURCE"], str(checkout)], check=True)
            uapi = (checkout / "uapi/supercall.h").read_bytes()
            builder.shell.cwd = str(checkout)
            builder._apply_patch_file(MOUNT_PATCH)
            builder._apply_patch_file(QUIET_PATCH)
            self.assertEqual((checkout / "uapi/supercall.h").read_bytes(), uapi)
            self.assertTrue((checkout / "kernel/feature/susfs.c").is_file())
            self.assertIn("include/quiet.h",
                          (checkout / "kernel/Kbuild").read_text(encoding="utf-8"))
            self.assertEqual(builder.applied_integration_patches,
                             [MOUNT_PATCH.name, QUIET_PATCH.name])
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(MOUNT_PATCH)


if __name__ == "__main__":
    unittest.main()
