#Requires -Version 5.1

<#
.SYNOPSIS
    Golem Dev Server — transparent launcher with environment setup and auto-restart.
.DESCRIPTION
    Sets up the dev environment (Python, uv, venv), starts the Golem UI server,
    and pipes all output live to the terminal. Auto-restarts on crash.
.PARAMETER Port
    Port to serve the dashboard on (default: 9664).
.PARAMETER HostAddr
    Host address to bind to (default: 127.0.0.1).
#>

param(
    [int]$Port = 9664,
    [string]$HostAddr = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$script:ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

$script:LogPath = Join-Path $script:ProjectRoot "golem-dashboard.log"

function Write-GolemLog {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    try { Add-Content -Path $script:LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
}

function Write-Check {
    param([string]$Status, [string]$Message, [ConsoleColor]$Color = [ConsoleColor]::White)
    Write-Host "  [$Status] " -ForegroundColor $Color -NoNewline
    Write-Host $Message
}

# ---------------------------------------------------------------------------
# Environment Setup
# ---------------------------------------------------------------------------

function Invoke-SetupChecks {
    Write-Host ""
    Write-Host "  Environment Checks" -ForegroundColor Cyan
    Write-Host "  ------------------" -ForegroundColor DarkGray

    # PowerShell version
    $psVer = "$($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor)"
    if ($PSVersionTable.PSVersion.Major -ge 5) {
        Write-Check "OK" "PowerShell $psVer" Green
    } else {
        Write-Check "FAIL" "PowerShell $psVer (need 5.1+)" Red
        return $false
    }

    # Python
    try {
        $pyOut = & python --version 2>&1
        $pyVer = ($pyOut -replace '[^0-9.]', '').Trim()
        $pyMajor = [int]($pyVer.Split('.')[0])
        $pyMinor = [int]($pyVer.Split('.')[1])
        if ($pyMajor -ge 3 -and $pyMinor -ge 12) {
            Write-Check "OK" "Python $pyVer" Green
        } else {
            Write-Check "FAIL" "Python $pyVer (need 3.12+)" Red
            return $false
        }
    } catch {
        Write-Check "FAIL" "Python not found" Red
        return $false
    }

    # uv
    try {
        $uvOut = & uv --version 2>&1
        Write-Check "OK" "$uvOut" Green
    } catch {
        Write-Check "FAIL" "uv not found" Red
        return $false
    }

    # venv
    $venvPython = Join-Path $script:ProjectRoot ".venv" "Scripts" "python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Check "WARN" "venv missing -- running uv sync..." Yellow
        Write-GolemLog "INFO" "Running uv sync"
        Push-Location $script:ProjectRoot
        try {
            & uv sync 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        } finally {
            Pop-Location
        }
        if (-not (Test-Path $venvPython)) {
            Write-Check "FAIL" "venv creation failed" Red
            return $false
        }
    }

    # Activate venv
    $venvScripts = Join-Path $script:ProjectRoot ".venv" "Scripts"
    if ($env:PATH -notlike "*$venvScripts*") {
        $env:PATH = "$venvScripts;$env:PATH"
    }
    Write-Check "OK" "venv activated" Green

    # Verify import
    try {
        & python -c "import golem" 2>&1 | Out-Null
        Write-Check "OK" "golem importable" Green
    } catch {
        Write-Check "FAIL" "golem import failed" Red
        return $false
    }

    # Port check
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect($HostAddr, $Port)
        $tcp.Close()
        # Port is busy
        try {
            $netstat = & netstat -ano 2>&1 | Select-String ":$Port\s" | Select-Object -First 1
            $pid_ = ($netstat -split '\s+')[-1]
            Write-Check "WARN" "Port $Port in use (PID $pid_) -- trying $($Port + 1)" Yellow
            $script:ActivePort = $Port + 1
        } catch {
            Write-Check "WARN" "Port $Port in use -- trying $($Port + 1)" Yellow
            $script:ActivePort = $Port + 1
        }
    } catch {
        # Port is free
        Write-Check "OK" "Port $Port available" Green
        $script:ActivePort = $Port
    }

    # Stale state
    $tasksJson = Join-Path $script:ProjectRoot ".golem" "tasks.json"
    if (Test-Path $tasksJson) {
        try {
            Get-Content $tasksJson -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
            Write-Check "OK" ".golem/tasks.json valid" Green
        } catch {
            Write-Check "WARN" ".golem/tasks.json corrupt -- run golem clean" Yellow
        }
    }

    Write-Host ""
    return $true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$script:ActivePort = $Port

Write-Host ""
Write-Host "  ========================================" -ForegroundColor DarkCyan
Write-Host "    GOLEM DEV SERVER" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor DarkCyan

$setupOk = Invoke-SetupChecks
if (-not $setupOk) {
    Write-Host ""
    Write-Host "  Setup failed. Fix the errors above and try again." -ForegroundColor Red
    Write-GolemLog "FATAL" "Setup checks failed"
    exit 1
}

Write-GolemLog "INFO" "Dashboard starting on $($HostAddr):$($script:ActivePort)"

Write-Host "  Server: http://$($HostAddr):$($script:ActivePort)" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# Set debug env var for golem.ui logger
$env:GOLEM_DEBUG = "1"

# Auto-restart loop
$crashCount = 0
$crashWindowStart = [DateTime]::Now

# Ctrl+C handler — kill the child process and exit cleanly
$script:ServerProc = $null

try {
    while ($true) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = "python"
        $psi.Arguments        = "-m uvicorn golem.ui:create_app --factory --host $HostAddr --port $($script:ActivePort) --log-level info"
        $psi.WorkingDirectory = $script:ProjectRoot
        $psi.UseShellExecute  = $false
        # Do NOT redirect -- let output flow directly to terminal
        $psi.RedirectStandardOutput = $false
        $psi.RedirectStandardError  = $false
        $psi.CreateNoWindow         = $false
        $psi.EnvironmentVariables["GOLEM_DEBUG"] = "1"

        try {
            $proc = [System.Diagnostics.Process]::Start($psi)
            $script:ServerProc = $proc
            Write-GolemLog "INFO" "Server started (PID $($proc.Id))"

            # Open browser on first launch
            if ($crashCount -eq 0) {
                Start-Sleep -Milliseconds 1500
                try { Start-Process "http://$($HostAddr):$($script:ActivePort)" } catch {}
            }

            # Poll instead of WaitForExit() so Ctrl+C can interrupt
            while (-not $proc.HasExited) {
                Start-Sleep -Milliseconds 300
            }
            $exitCode = $proc.ExitCode
            $script:ServerProc = $null

            Write-GolemLog "INFO" "Server exited with code $exitCode"

            if ($exitCode -eq 0) {
                Write-Host ""
                Write-Host "  Server stopped cleanly." -ForegroundColor Green
                break
            }

            # Crash handling
            $now = [DateTime]::Now
            if (($now - $crashWindowStart).TotalSeconds -gt 60) {
                $crashCount = 0
                $crashWindowStart = $now
            }
            $crashCount++

            if ($crashCount -ge 3) {
                Write-Host ""
                Write-Host "  Server crashed $crashCount times in 60s. Giving up." -ForegroundColor Red
                Write-GolemLog "FATAL" "Server crashed $crashCount times in 60s"
                break
            }

            Write-Host ""
            Write-Host "  Server crashed (exit code $exitCode). Restarting... ($crashCount/3)" -ForegroundColor Yellow
            Write-GolemLog "WARN" "Server crashed (exit=$exitCode), restart $crashCount/3"
            Start-Sleep -Seconds 2

        } catch {
            Write-Host "  Failed to start server: $_" -ForegroundColor Red
            Write-GolemLog "ERROR" "Failed to start: $_"
            break
        }
    }
} finally {
    # Ensure child process is killed on Ctrl+C or any exit
    if ($script:ServerProc -and -not $script:ServerProc.HasExited) {
        Write-Host ""
        Write-Host "  Stopping server (PID $($script:ServerProc.Id))..." -ForegroundColor Yellow
        try {
            $script:ServerProc.Kill()
            $script:ServerProc.WaitForExit(3000) | Out-Null
        } catch {}
        Write-GolemLog "INFO" "Server killed by Ctrl+C"
    }
}

Write-Host ""
Write-Host "  Golem dev server stopped." -ForegroundColor Cyan
Write-GolemLog "INFO" "Dashboard stopped"
