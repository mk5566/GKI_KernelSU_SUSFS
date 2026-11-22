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
    # Prefer a separate backing disk when the runner provides one.
    mnt_avail_g=0
    if mountpoint -q /mnt; then
      mnt_avail_g="$(df -BG --output=avail /mnt | tail -1 | tr -dc '0-9')"
    fi
    root_avail_g="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"

    swap_size=16G
    if (( mnt_avail_g >= 32 )); then
      swap_path=/mnt/gki-link.swap
      swap_size=20G
    elif (( mnt_avail_g >= 14 )); then
      swap_path=/mnt/gki-link.swap
      if (( mnt_avail_g < 18 )); then
        swap_size=12G
      fi
    else
      swap_path=/tmp/gki-link.swap
      if (( root_avail_g < 54 )); then
        swap_size=12G
      fi
    fi
    if [[ -e "$swap_path" || -L "$swap_path" ]]; then
      echo "Refusing to replace existing swap: $swap_path" >&2
      exit 1
    fi
    sudo install -m 600 /dev/null "$swap_path"
    sudo fallocate -l "$swap_size" "$swap_path"
    sudo mkswap "$swap_path"
    sudo swapon -p 10 "$swap_path"
    echo "GKI_CI_SWAP_LIMIT=$swap_size" >> "$GITHUB_ENV"

    if mountpoint -q /mnt && [ "$(df -BG --output=avail /mnt | tail -1 | tr -dc '0-9')" -ge 10 ]; then
      sudo install -d -o "$(id -u)" -g "$(id -g)" -m 1777 /mnt/gki-tmp
      echo "TMPDIR=/mnt/gki-tmp" >> "$GITHUB_ENV"
    fi

    df -h / /tmp /mnt || true
    free -h
    swapon --show
    ;;
  build)
    shift
    if [[ ! -r /sys/fs/cgroup/cgroup.controllers ]] ||
       ! grep -qw memory /sys/fs/cgroup/cgroup.controllers; then
      echo "Kernel build requires a cgroup v2 memory controller" >&2
      exit 1
    fi
    : "${GKI_CI_SWAP_LIMIT:?Run CI disk swap provisioning before the build}"
    resources="$RUNNER_TEMP/gki-runner-resources.txt"
    unit="gki-build-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.scope"
    group="/sys/fs/cgroup/system.slice/$unit"
    started="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

    sample_resources() {
      {
        date -u '+[runner resources] %FT%TZ'
        awk '/^(MemAvailable|SwapFree):/' /proc/meminfo
        df -Ph "$GKI_WORKSPACE" "${TMPDIR:-/tmp}" /
        for counter in memory.current memory.peak memory.swap.current memory.events memory.pressure io.pressure; do
          if [[ -r "$group/$counter" ]]; then
            awk -v name="$counter" '{ print name ": " $0 }' "$group/$counter"
          fi
        done
      } 2>&1 | tee -a "$resources"
    }

    sample_resources
    # Leave headroom for the runner and OS, and reclaim inside the build scope
    # before it reaches the hard limit. Drop root before running the builder.
    sudo -n --preserve-env systemd-run --scope --slice=system.slice --unit="$unit" \
      --expand-environment=no \
      --property=MemoryHigh=75% --property=MemoryMax=85% \
      --property="MemorySwapMax=$GKI_CI_SWAP_LIMIT" --property=OOMPolicy=kill \
      --property=CPUWeight=25 --property=IOWeight=25 --property=TimeoutStopSec=3s \
      /usr/bin/setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups -- \
      /usr/bin/env "HOME=$HOME" "PATH=$PATH" "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" \
      nice -n 10 ionice -c 2 -n 7 python3 .github/workflows/scripts/build.py "$@" &
    build_pid=$!
    (
      while true; do
        sleep 30 >/dev/null &
        timer_pid=$!
        trap 'kill "$timer_pid" 2>/dev/null || true; exit' TERM
        wait "$timer_pid"
        sample_resources || true
      done
    ) &
    monitor_pid=$!
    trap 'kill "$monitor_pid" 2>/dev/null || true; wait "$monitor_pid" || true' EXIT
    trap 'sudo -n systemctl stop "$unit" || true; exit 130' INT
    trap 'sudo -n systemctl stop "$unit" || true; exit 143' TERM

    result=0
    wait "$build_pid" || result=$?
    sample_resources || true
    if (( result != 0 )); then
      {
        sudo -n journalctl -k --since "$started" --no-pager \
          --grep 'oom-kill|[Oo]ut of memory|Killed process' || true
        sudo -n journalctl -u "$unit" --since "$started" --no-pager || true
      } 2>&1 | tee -a "$resources" || true
    fi
    exit "$result"
    ;;
  *) echo "Usage: $0 dependencies|memory|build [build arguments]" >&2; exit 2 ;;
esac
