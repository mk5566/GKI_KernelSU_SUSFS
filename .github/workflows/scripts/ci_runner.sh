#!/usr/bin/env bash
# Destructive provisioning for this workflow's disposable GitHub-hosted runner.
# Never run on a development machine or a persistent/self-hosted runner.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != "true" || "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]]; then
  echo "ci_runner.sh requires a disposable GitHub-hosted Actions runner" >&2
  exit 1
fi
case "${1:-}" in
  dependencies)
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends \
      build-essential clang git curl zip unzip xz-utils openssl rsync bc bison flex \
      libssl-dev libelf-dev lz4 kmod zstd cpio
    sudo apt-get clean
    sudo rm -rf \
      /var/lib/apt/lists/* \
      /usr/local/lib/android \
      /opt/ghc \
      /usr/local/.ghcup \
      /usr/share/swift \
      /usr/share/dotnet \
      /opt/hostedtoolcache/CodeQL \
      /usr/local/share/boost \
      /usr/local/share/chromium
    if command -v docker >/dev/null 2>&1; then
      sudo docker system prune -af --volumes || true
    fi
    df -h / /tmp /mnt || true
    ;;
  memory)
    set -euo pipefail
    # Prevent Ubuntu 24.04 systemd-oomd from killing the runner service cgroup
    # on memory/swap PSI pressure during full LTO linking.
    if systemctl is-active --quiet systemd-oomd 2>/dev/null; then
      sudo systemctl stop systemd-oomd || true
    fi

    # Protect the Actions runner, VM agent, and system logging from kernel OOM
    # kill so memory exhaustion terminates the build step rather than severing
    # runner communication with GitHub.
    for proc_pattern in 'Runner\.Listener' 'Runner\.Worker' 'waagent' 'WALinuxAgent' 'systemd-journald'; do
      for pid in $(pgrep -f "$proc_pattern" 2>/dev/null || true); do
        echo -900 | sudo tee "/proc/$pid/oom_score_adj" >/dev/null 2>&1 || true
      done
    done

    # Cap compressed in-RAM zram swap at 1/4 RAM (max 4 GiB, prefer low-CPU lz4)
    # so LTO keeps maximum physical RAM for its active working set and runner
    # processes without CPU starvation on zstd compression.
    zram_active=0
    if sudo modprobe zram 2>/dev/null; then
      mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
      zram_bytes="$(( mem_kb * 1024 / 4 ))"
      max_zram="$(( 4 * 1024 * 1024 * 1024 ))"
      if [ "$zram_bytes" -gt "$max_zram" ]; then
        zram_bytes="$max_zram"
      fi
      zram_dev="$(sudo zramctl --find --size "$zram_bytes" --algorithm lz4 2>/dev/null || sudo zramctl --find --size "$zram_bytes" --algorithm zstd 2>/dev/null || true)"
      if [ -n "$zram_dev" ]; then
        sudo mkswap "$zram_dev"
        sudo swapon -p 100 "$zram_dev"
        zram_active=1
      fi
    fi

    # Provision backing disk swap (priority 10) on /mnt when space allows so /
    # stays clear for GKI_WORKSPACE.
    mnt_avail_g=0
    if [ -d /mnt ]; then
      mnt_avail_g="$(df -BG --output=avail /mnt | tail -1 | tr -dc '0-9')"
    fi
    root_avail_g="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"

    swap_size="16G"
    if [ "${mnt_avail_g:-0}" -ge 32 ]; then
      swap_path="/mnt/gki-link.swap"
      swap_size="20G"
    elif [ "${mnt_avail_g:-0}" -ge 18 ]; then
      swap_path="/mnt/gki-link.swap"
    elif [ "${mnt_avail_g:-0}" -ge 14 ]; then
      swap_path="/mnt/gki-link.swap"
      swap_size="12G"
    else
      swap_path="/tmp/gki-link.swap"
      if [ "${root_avail_g:-0}" -lt 54 ]; then
        swap_size="12G"
      fi
    fi
    if [ -e "$swap_path" ] || [ -L "$swap_path" ]; then
      echo "Refusing to replace existing swap: $swap_path" >&2
      exit 1
    fi
    # Private permissions before allocation, not after writing to a public file.
    sudo install -m 600 /dev/null "$swap_path"
    sudo fallocate -l "$swap_size" "$swap_path"
    sudo mkswap "$swap_path"
    sudo swapon -p 10 "$swap_path"

    # Avoid aggressively swapping out idle runner/system pages (swappiness=100)
    # while still allowing cold LTO allocations to page out under pressure.
    if [ "$zram_active" -eq 1 ]; then
      sudo sysctl -w vm.swappiness=20 vm.page-cluster=0
    else
      sudo sysctl -w vm.swappiness=20
    fi
    if [ "$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)" -ge 4194304 ]; then
      sudo sysctl -w vm.min_free_kbytes=65536
    fi

    if [ -d /mnt ] && [ "$(df -BG --output=avail /mnt | tail -1 | tr -dc '0-9')" -ge 10 ]; then
      sudo install -d -o "$(id -u)" -g "$(id -g)" -m 1777 /mnt/gki-tmp
      echo "TMPDIR=/mnt/gki-tmp" >> "$GITHUB_ENV"
    fi

    df -h / /tmp /mnt || true
    free -h
    swapon --show || true
    ;;
  *) echo "Usage: $0 dependencies|memory" >&2; exit 2 ;;
esac
