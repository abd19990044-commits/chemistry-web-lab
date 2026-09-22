# Chemistry Lab Windows Bootstrap Helper
$ErrorActionPreference = "Stop"
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host " Chemistry Lab Local Agent (Windows PowerShell)" -ForegroundColor Cyan
Write-Host "==============================================================="

$AgentDir = Join-Path $env:LOCALAPPDATA "ChemistryLabAgent"
$VenvDir = Join-Path $AgentDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Test-SupportedPython {
    param([string]$ExePath)
    if (-not (Test-Path $ExePath)) { return $false }
    try {
        & $ExePath -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# 1. Check existing venv
$venvReady = $false
if (Test-Path $VenvPython) {
    if (Test-SupportedPython $VenvPython) {
        $venvReady = $true
    } else {
        Write-Warning "Existing virtual environment Python does not satisfy >=3.11, <3.14. Recreating..."
        Remove-Item -Recurse -Force $VenvDir -ErrorAction SilentlyContinue
    }
}

if (-not $venvReady) {
    if (-not (Test-Path $AgentDir)) {
        New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null
    }

    # 2. Discover supported Python interpreter (>=3.11, <3.14)
    $selectedExe = $null

    # Query py launcher if present
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($pyTag in @("3.12", "3.13", "3.11")) {
            try {
                $testOut = & py -$pyTag -c "import sys; sys.exit(0)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $selectedExe = "py -$pyTag"
                    break
                }
            } catch {}
        }
        if (-not $selectedExe) {
            $pyList = & py -0p 2>$null
            foreach ($line in $pyList) {
                if ($line -match '^\s*-V:([0-9]+\.[0-9]+)\s*\*?\s*(.+)$') {
                    $verStr = $matches[1]
                    $exePath = $matches[2].Trim()
                    try {
                        $ver = [version]$verStr
                        if ($ver -ge [version]"3.11" -and $ver -lt [version]"3.14") {
                            if (Test-SupportedPython $exePath) {
                                $selectedExe = $exePath
                                break
                            }
                        }
                    } catch {}
                }
            }
        }
    }

    # Fallback to PATH commands
    if (-not $selectedExe) {
        foreach ($name in @("python.exe", "python3.exe")) {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd) {
                if (Test-SupportedPython $cmd.Source) {
                    $selectedExe = $cmd.Source
                    break
                }
            }
        }
    }

    if (-not $selectedExe) {
        Write-Host "===============================================================" -ForegroundColor Red
        Write-Host " [ERROR] Supported Python (>=3.11, <3.14) not found." -ForegroundColor Red
        Write-Host " Chemistry Lab requires Python 3.11, 3.12, or 3.13." -ForegroundColor Red
        Write-Host " Python 3.14 is currently unsupported." -ForegroundColor Red
        Write-Host "===============================================================" -ForegroundColor Red
        $response = Read-Host "Would you like to install Python 3.12 via winget? [Y/N]"
        if ($response -match "^[Yy]$") {
            Write-Host "[*] Installing Python 3.12 via winget..."
            winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            Write-Host "Installation completed. Please re-run bootstrap_windows.ps1."
            exit 0
        } else {
            Write-Error "Python 3.11-3.13 is required to run the Chemistry Lab agent."
            exit 1
        }
    }

    Write-Host "[*] Creating dedicated virtual environment using $selectedExe..."
    if ($selectedExe -like "py -*") {
        $tag = $selectedExe.Replace("py ", "")
        & py $tag -m venv $VenvDir
    } else {
        & $selectedExe -m venv $VenvDir
    }
}

Write-Host "[*] Installing dependencies..."
& $VenvPython -m pip install --quiet --upgrade pip

$reqFile = Join-Path $PSScriptRoot "requirements-local-agent.txt"
if (-not (Test-Path $reqFile)) {
    $reqFile = Join-Path $PSScriptRoot "..\..\requirements-local-agent.txt"
}
if (Test-Path $reqFile) {
    & $VenvPython -m pip install --quiet -r $reqFile
}

# Ensure local_agent module is on PYTHONPATH
if (Test-Path (Join-Path $PSScriptRoot "local_agent")) {
    $env:PYTHONPATH = "$PSScriptRoot;$env:PYTHONPATH"
} elseif (Test-Path (Join-Path $PSScriptRoot "..\..\local_agent")) {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $env:PYTHONPATH = "$repoRoot;$env:PYTHONPATH"
}

Write-Host "[*] Starting Agent..."
& $VenvPython -m local_agent.agent start

