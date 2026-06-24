# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
#
# Miloco Installer — PowerShell Bootstrap
# Ensures uv + Python are available, then delegates to install.py.
#
# Usage: .\scripts\install.ps1 [options]
# Options are forwarded to install.py (--dev, --lang, --omni-api-key, --uninstall, -h)

$ErrorActionPreference = "Stop"

function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Write-Fail  { Write-Host "[FAIL]  $args" -ForegroundColor Red; exit 1 }

# ── Step 1: Ensure uv is available ────────────────────────
function Ensure-Uv {
    $candidates = @("uv")
    $localBin = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"
    if (Test-Path $localBin) { $candidates += $localBin }
    if (Test-Path $cargoBin) { $candidates += $cargoBin }

    foreach ($p in $candidates) {
        try {
            $null = & $p --version 2>$null
            if ($LASTEXITCODE -eq 0) {
                $script:UvCmd = $p
                return
            }
        } catch {}
    }

    Write-Info "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

    # Refresh PATH
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")

    try {
        $null = & uv --version 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:UvCmd = "uv"
            return
        }
    } catch {}
    Write-Fail "uv installation failed"
}

# ── Step 2: Ensure Python >=3.11 (prefer 3.14) ───────────
function Ensure-Python {
    foreach ($ver in @("3.14", "3.13", "3.12", "3.11")) {
        $found = & $script:UvCmd python find $ver 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) {
            Write-Info "Python $ver found: $found"
            return
        }
    }
    Write-Info "Installing Python 3.14 via uv..."
    & $script:UvCmd python install 3.14
    $found = & $script:UvCmd python find 3.14 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Fail "Python installation failed" }
}

# ── Step 3: Ensure user bin directory is on PATH ──────────
function Ensure-UserBinOnPath {
    $target = Join-Path $env:USERPROFILE ".local\bin"
    if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }

    if ($env:PATH -notlike "*$target*") {
        $env:PATH = "$target;$env:PATH"
    }

    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$target*") {
        [Environment]::SetEnvironmentVariable("PATH", "$target;$userPath", "User")
        Write-Info "Added $target to user PATH"
    }
}

# ── Run ──────────────────────────────────────────────────
Write-Host ""
Write-Host "[FAIL] Please install Miloco inside WSL (Windows Subsystem for Linux)." -ForegroundColor Red
exit 1

# NOTE: Windows native install is currently unsupported, so we exit above.
# The installation logic below (embedded install.py, download flow, etc.) is
# intentionally preserved for if/when native Windows support is restored.

# __SELF_CONTAINED__ — build.sh inserts resource extraction here
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Ensure-Uv
Ensure-Python
Ensure-UserBinOnPath
& $script:UvCmd run "$ScriptDir\install.py" @args
exit $LASTEXITCODE
