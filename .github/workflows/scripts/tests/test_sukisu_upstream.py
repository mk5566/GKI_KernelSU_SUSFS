"""Keep unrelated upstream features out of required integration context."""
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
    def test_optional_features_survive_mount_integration(self):
        # Relevant source excerpts from upstream 0bca5d15. The setuid excerpt
        # omits unrelated allowlist handling between the manager and umount.
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
        setuid = """#include "supercall/supercall.h"
#include "hook/tp_marker.h"
#include "feature/kernel_umount.h"

int ksu_handle_setresuid(uid_t old_uid, uid_t new_uid)
{
    // we rely on the fact that zygote always call setresuid(3) with same uids

    pr_info("handle_setresuid from %d to %d\\n", old_uid, new_uid);

    if (unlikely(is_uid_manager(new_uid))) {
        spin_lock_irq(&current->sighand->siglock);
        ksu_seccomp_allow_cache(current->seccomp.filter, __NR_reboot);
        ksu_set_task_tracepoint_flag(current);
        spin_unlock_irq(&current->sighand->siglock);

        pr_info("install fd for manager: %d\\n", new_uid);
        ksu_install_fd();
        return 0;
    }

    // Handle kernel umount
    ksu_handle_umount(old_uid, new_uid);

    return 0;
}
"""
        # Old layout, the real October 18 additions, and further unrelated
        # neighbours. Never fix this by enumerating optional upstream features.
        for extras in ((), ("mount_hide",), ("mount_hide", "future_feature")):
            with self.subTest(extras=extras):
                self._check_optional_features(kbuild, boot, setuid, extras)

    def _check_optional_features(self, kbuild, boot, setuid, extras):
        kbuild = kbuild.replace("kernelsu-objs += feature/sulog.o",
                               "".join(f"kernelsu-objs += feature/{name}.o\n" for name in extras)
                               + "kernelsu-objs += feature/sulog.o")
        setuid = setuid.replace('#include "feature/kernel_umount.h"',
                                '#include "feature/kernel_umount.h"' +
                                "".join(f'\n#include "feature/{name}.h"' for name in extras))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "kernel/runtime").mkdir(parents=True)
            (root / "kernel/hook").mkdir(parents=True)
            (root / "kernel/Kbuild").write_text(kbuild, encoding="utf-8")
            (root / "kernel/runtime/boot_event.c").write_text(boot, encoding="utf-8")
            (root / "kernel/hook/setuid_hook.c").write_text(setuid, encoding="utf-8")
            sections = MOUNT_PATCH.read_text(encoding="utf-8").split("diff --git ")
            selected = "".join("diff --git " + section for section in sections[1:]
                               if section.startswith(("a/kernel/Kbuild ",
                                                      "a/kernel/hook/setuid_hook.c ",
                                                      "a/kernel/runtime/boot_event.c ")))
            patch_file = root / "layout.patch"
            patch_file.write_text(selected, encoding="utf-8")
            builder = KernelBuilder(BuildConfig(), temp)
            builder.shell.cwd = str(root)
            builder._apply_patch_file(patch_file)
            result = (root / "kernel/Kbuild").read_text(encoding="utf-8")
            self.assertIn("kernelsu-$(CONFIG_KSU_SUSFS) += feature/susfs.o", result)
            self.assertIn("kernelsu-objs += feature/module_blacklist.o", result)
            self.assertEqual(result.replace("kernelsu-$(CONFIG_KSU_SUSFS) += feature/susfs.o\n", ""),
                             kbuild)
            result = (root / "kernel/hook/setuid_hook.c").read_text(encoding="utf-8")
            for addition in ('#include "feature/susfs.h"\n',
                             "        ksu_susfs_clear_task();\n",
                             "    ksu_susfs_setuid(new_uid);\n"):
                self.assertEqual(result.count(addition), 1)
                result = result.replace(addition, "")
            self.assertEqual(result, setuid)
            result = (root / "kernel/runtime/boot_event.c").read_text(encoding="utf-8")
            self.assertIn('#include "policy/feature.h"', result)
            self.assertIn("#include <linux/susfs.h>", result)
            self.assertIn("susfs_start_sdcard_monitor_fn();", result)
            self.assertLess(result.index("ksu_feature_apply_forced();"),
                            result.index("susfs_start_sdcard_monitor_fn();"))

            # An actual contract change must fail without partially inserting
            # the build object or boot hook. Benign neighbouring churn is OK.
            for relative, original in (("kernel/Kbuild", kbuild),
                                       ("kernel/runtime/boot_event.c", boot),
                                       ("kernel/hook/setuid_hook.c", setuid)):
                (root / relative).write_text(original, encoding="utf-8")
            hook = root / "kernel/hook/setuid_hook.c"
            broken = setuid.replace("ksu_handle_umount(old_uid, new_uid);",
                                    "ksu_changed_umount(old_uid, new_uid);")
            hook.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(patch_file)
            self.assertEqual(hook.read_text(encoding="utf-8"), broken)
            self.assertEqual((root / "kernel/Kbuild").read_text(encoding="utf-8"), kbuild)
            self.assertEqual((root / "kernel/runtime/boot_event.c").read_text(encoding="utf-8"), boot)

    def test_quiet_patch_preserves_adjacent_build_rules(self):
        flags = "ccflags-y += -I$(KSU_KERNEL_DIR) -I$(KSU_KERNEL_DIR)/include\n"
        for rules in ("", "# preinit runner\nkernelsu-objs += preinit/preinit.o\n\n",
                      "# future independent rules\nkernelsu-objs += future.o\n\n"):
            with self.subTest(rules=rules), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                (root / "kernel/include").mkdir(parents=True)
                original = "endif\n\n" + flags + "\n" + rules + "obj-$(CONFIG_KSU) += kernelsu.o\n\n"
                kbuild = root / "kernel/Kbuild"
                kbuild.write_text(original, encoding="utf-8")
                builder = KernelBuilder(BuildConfig(), temp)
                builder.shell.cwd = str(root)
                builder._apply_patch_file(QUIET_PATCH)
                self.assertEqual(kbuild.read_text(encoding="utf-8").replace(
                    "ccflags-y += -include $(KSU_KERNEL_DIR)/include/quiet.h\n", ""), original)
                self.assertTrue((root / "kernel/include/quiet.h").is_file())

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
