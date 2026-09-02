#Requires -Version 5.1
<#
.SYNOPSIS
  Apply the locked android13-5.15.180 slim profile onto an existing GKI_KernelSU_SUSFS clone.

.DESCRIPTION
  Compatible with Windows PowerShell 5.1 and PowerShell 7+.
  Copies the sibling slim-payload/ tree into the current git repo, deletes
  OnePlus/multi-version files, then shows git status / diff --stat.
  Default is review-only (no file writes) unless -Apply is passed.
  Never force-pushes unless -ForcePush is explicitly passed.

.PARAMETER Apply
  Write files onto the current repo.

.PARAMETER Commit
  After a successful -Apply, create a commit on slim-5.15.180-sukisu.

.PARAMETER Push
  After a successful -Commit, push the branch. Refused if commit did not happen.

.PARAMETER Remote
  Git remote name. Default: origin.

.PARAMETER ForcePush
  Allow git push --force-with-lease. Prints a warning. Off by default.

.PARAMETER Branch
  Branch to create/switch to. Default: slim-5.15.180-sukisu.
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Commit,
    [switch]$Push,
    [string]$Remote = "origin",
    [switch]$ForcePush,
    [string]$Branch = "slim-5.15.180-sukisu"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
try { chcp 65001 > $null } catch { }

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-GitRepo {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is not on PATH. Install Git for Windows and retry."
    }
    $inside = & git rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside.Trim() -ne "true") {
        throw "Current directory is not a git repository. cd into your GKI_KernelSU_SUSFS clone first."
    }
}

function Get-PayloadRoot {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $candidate = Join-Path $here "slim-payload"
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "Payload folder not found: $candidate`nCopy apply-gki-515180-slim.ps1 together with slim-payload\ into the same directory."
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

$Deletes = @(
    "hmbird_patch.c",
    ".github/workflows/build-kernels.yml"
)

$PatchMappings = @(
    @{ Src = "avoid_extra_s2idle_wake_attempts.patch"; Dst = "patches/5.15.180/avoid_extra_s2idle_wake_attempts.patch" },
    @{ Src = "minimise_wakeup_time.patch"; Dst = "patches/5.15.180/minimise_wakeup_time.patch" },
    @{ Src = "reduce_freeze_timeout.patch"; Dst = "patches/5.15.180/reduce_freeze_timeout.patch" },
    @{ Src = "clear_page_16bytes_align.patch"; Dst = "patches/5.15.180/clear_page_16bytes_align.patch" },
    @{ Src = "f2fs_reduce_congestion.patch"; Dst = "patches/5.15.180/f2fs_reduce_congestion.patch" },
    @{ Src = "disable_cache_hot_buddy.patch"; Dst = "patches/5.15.180/disable_cache_hot_buddy.patch" },
    @{ Src = "silence_irq_cpu_logspam.patch"; Dst = "patches/5.15.180/silence_irq_cpu_logspam.patch" },
    @{ Src = "f2fs_enlarge_min_fsync_blocks.patch"; Dst = "patches/5.15.180/f2fs_enlarge_min_fsync_blocks.patch" },
    @{ Src = "adjust_cpu_scan_order.patch"; Dst = "patches/5.15.180/adjust_cpu_scan_order.patch" },
    @{ Src = "optimise_memcmp.patch"; Dst = "patches/5.15.180/optimise_memcmp.patch" },
    @{ Src = "file_struct_8bytes_align.patch"; Dst = "patches/5.15.180/file_struct_8bytes_align.patch" },
    @{ Src = "mem_opt_prefetch.patch"; Dst = "patches/5.15.180/mem_opt_prefetch.patch" },
    @{ Src = "int_sqrt.patch"; Dst = "patches/5.15.180/int_sqrt.patch" },
    @{ Src = "increase_sk_mem_packets.patch"; Dst = "patches/5.15.180/increase_sk_mem_packets.patch" },
    @{ Src = "reduce_cache_pressure.patch"; Dst = "patches/5.15.180/reduce_cache_pressure.patch" },
    @{ Src = "reduce_gc_thread_sleep_time.patch"; Dst = "patches/5.15.180/reduce_gc_thread_sleep_time.patch" },
    @{ Src = "increase_ext4_default_commit_age.patch"; Dst = "patches/5.15.180/increase_ext4_default_commit_age.patch" },
    @{ Src = "reduce_pci_pme_wakeups.patch"; Dst = "patches/5.15.180/reduce_pci_pme_wakeups.patch" },
    @{ Src = "silence_system_logspam.patch"; Dst = "patches/5.15.180/silence_system_logspam.patch" },
    @{ Src = "bbrv3/0001-net-tcp-backport-BBRv3-to-android13-5.15.patch"; Dst = "patches/5.15.180/0001-net-tcp-backport-BBRv3-to-android13-5.15.patch" }
)

$ApplyOrder = @(
    "# Patch apply order for android13-5.15.180",
    "# Lines starting with ! are required: a failed apply fails the build.",
    "avoid_extra_s2idle_wake_attempts.patch",
    "minimise_wakeup_time.patch",
    "reduce_freeze_timeout.patch",
    "clear_page_16bytes_align.patch",
    "file_struct_8bytes_align.patch",
    "f2fs_reduce_congestion.patch",
    "f2fs_enlarge_min_fsync_blocks.patch",
    "reduce_gc_thread_sleep_time.patch",
    "disable_cache_hot_buddy.patch",
    "adjust_cpu_scan_order.patch",
    "optimise_memcmp.patch",
    "mem_opt_prefetch.patch",
    "int_sqrt.patch",
    "increase_sk_mem_packets.patch",
    "reduce_cache_pressure.patch",
    "increase_ext4_default_commit_age.patch",
    "reduce_pci_pme_wakeups.patch",
    "silence_irq_cpu_logspam.patch",
    "silence_system_logspam.patch",
    "!0001-net-tcp-backport-BBRv3-to-android13-5.15.patch"
)

Write-Host "Windows apply script for locked GKI 5.15.180 slim profile" -ForegroundColor Green
Write-Host "cwd: $(Get-Location)"
Write-Host "PowerShell: $($PSVersionTable.PSVersion)"
Write-Host ""
Write-Host "Planned commands:" -ForegroundColor Yellow
Write-Host "  git rev-parse --is-inside-work-tree"
Write-Host "  git checkout -B $Branch"
if ($Apply) {
    Write-Host "  copy slim-payload patches into patches/5.15.180/"
    Write-Host "  generate patches/5.15.180/APPLY_ORDER.txt"
    Write-Host "  delete: $($Deletes -join ', ')"
}
Write-Host "  git status --short"
Write-Host "  git diff --stat HEAD"
if ($Commit) { Write-Host "  git add -A ; git commit" }
if ($Push -and $ForcePush) { Write-Host "  git push --force-with-lease $Remote $Branch" }
elseif ($Push) { Write-Host "  git push -u $Remote $Branch" }
Write-Host ""

Assert-GitRepo
$payload = Get-PayloadRoot
Write-Step "Payload: $payload"

$existing = & git branch --list $Branch
if ($existing) {
    Write-Step "Switching to existing branch $Branch"
    & git checkout $Branch
} else {
    Write-Step "Creating branch $Branch"
    & git checkout -B $Branch
}
if ($LASTEXITCODE -ne 0) { throw "git checkout failed" }

if (-not $Apply) {
    Write-Host ""
    Write-Host "Review mode. No files were written." -ForegroundColor Yellow
    Write-Host "Re-run with -Apply to write the slim profile."
    Write-Host "Expected patch writes ($($PatchMappings.Count)):"
    $PatchMappings | ForEach-Object { Write-Host "  + $($_.Dst)" }
    Write-Host "Expected deletes:"
    $Deletes | ForEach-Object { Write-Host "  - $_" }
    & git status --short
    exit 0
}

Write-Step "Writing payload files"
foreach ($item in $PatchMappings) {
    $src = Join-Path $payload $item.Src
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Payload file missing: $src"
    }
    $dst = Join-Path (Get-Location) $item.Dst
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) {
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    Write-Host "  wrote $($item.Dst)"
}

$orderFilePath = Join-Path (Get-Location) "patches/5.15.180/APPLY_ORDER.txt"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($orderFilePath, (($ApplyOrder -join "`n") + "`n"), $utf8NoBom)
Write-Host "  generated patches/5.15.180/APPLY_ORDER.txt"

Write-Step "Deleting removed files"
foreach ($rel in $Deletes) {
    $dst = Join-Path (Get-Location) $rel
    if (Test-Path -LiteralPath $dst) {
        Remove-Item -LiteralPath $dst -Force
        Write-Host "  deleted $rel"
    } else {
        Write-Host "  already absent $rel"
    }
}

Write-Step "git status --short"
& git status --short
Write-Step "git diff --stat HEAD"
& git diff --stat HEAD
& git diff --cached --stat

$script:CommitSucceeded = $false
if ($Commit) {
    Write-Step "Creating commit"
    & git add -A
    $msg = @"
Slim repo to locked android13-5.15.180 SukiSU+SUSFS profile

- Single matrix entry: android13-5.15.180 / 2025-05
- Vendor WildKernels common YES+OPTIONAL patches under patches/5.15.180
- Defaults: ZRAM LZ4KD on, KPM off, BBG off, OnePlus/hmbird off, BBR on
- Translate user-facing text to English
- Remove multi-version Build Kernels workflow
"@
    & git commit -m $msg
    if ($LASTEXITCODE -ne 0) {
        throw "git commit failed (nothing to commit, or hook rejected)."
    }
    $script:CommitSucceeded = $true
    Write-Host "Commit created." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Stopped before commit (pass -Commit to commit)." -ForegroundColor Yellow
}

if ($Push) {
    if (-not $script:CommitSucceeded) {
        throw "-Push refused: -Commit did not succeed in this run."
    }
    if ($ForcePush) {
        Write-Host "WARNING: -ForcePush uses --force-with-lease and can rewrite the remote branch." -ForegroundColor Red
        & git push --force-with-lease -u $Remote $Branch
    } else {
        & git push -u $Remote $Branch
    }
    if ($LASTEXITCODE -ne 0) { throw "git push failed" }
}

Write-Host ""
Write-Host "Done. Inspect git diff, then build on GitHub Actions (Kernel Build)." -ForegroundColor Green
