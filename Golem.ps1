#Requires -Version 5.1

<#
.SYNOPSIS
    Golem Multi-Session Server — one-click launcher with environment setup and auto-restart.
.DESCRIPTION
    Sets up the dev environment (Python, uv, venv), verifies all required tools
    (git, claude CLI, rg, gh), starts the Golem multi-session server
    (golem.server:create_app), writes .golem/server.json for CLI discovery,
    polls /api/server/status for readiness, opens the dashboard in the browser,
    and auto-restarts on crash with diagnostics.

    Refreshes PATH from the Windows registry before checking tools, so tools
    installed after the current shell was opened are found automatically.
.PARAMETER Port
    Port to serve the dashboard on (default: 7665).
.PARAMETER HostAddr
    Host address to bind to (default: 127.0.0.1).
.PARAMETER Clean
    Wipe .golem/ state before starting (equivalent to golem clean).
    For full cleanup including git worktree branches, use: uv run golem clean
#>

param(
    [int]$Port = 7665,
    [string]$HostAddr = "127.0.0.1",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$script:ProjectRoot       = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:ActivePort        = $Port
$script:ServerProc        = $null
$script:RecoveredSessions = @()
$script:GolemDir          = Join-Path $script:ProjectRoot ".golem"
$script:ServerJsonPath    = Join-Path $script:GolemDir "server.json"

# ---------------------------------------------------------------------------
# Logging helpers
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
# PATH refresh — mirrors validator.py _subprocess_env() for stale-PATH fix
# ---------------------------------------------------------------------------

function Sync-PathFromRegistry {
    <#
    .SYNOPSIS
        Merge fresh User + Machine PATH from Windows registry into the current
        session, so tools installed after this shell was opened are found.
        This mirrors the pattern in src/golem/validator.py _subprocess_env().
    #>
    try {
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")

        # Build a lookup of dirs already on the current PATH
        $currentDirs = @{}
        foreach ($d in ($env:PATH -split ';')) {
            $norm = $d.TrimEnd('\', '/').ToLowerInvariant()
            if ($norm) { $currentDirs[$norm] = $true }
        }

        # Prepend any registry entries that are missing from the current session
        $added = @()
        foreach ($source in @($userPath, $machinePath)) {
            if (-not $source) { continue }
            foreach ($d in ($source -split ';')) {
                $norm = $d.TrimEnd('\', '/').ToLowerInvariant()
                if ($norm -and -not $currentDirs.ContainsKey($norm)) {
                    $currentDirs[$norm] = $true
                    $added += $d
                }
            }
        }

        if ($added.Count -gt 0) {
            $env:PATH = ($added -join ';') + ';' + $env:PATH
            Write-GolemLog "INFO" "PATH refresh: added $($added.Count) dir(s) from registry"
        }
    } catch {
        Write-GolemLog "WARN" "PATH refresh failed: $_"
    }
}

# ---------------------------------------------------------------------------
# Tool resolution helper
# ---------------------------------------------------------------------------

function Find-Tool {
    <#
    .SYNOPSIS
        Locate a tool by name. Returns the full path or $null.
        Uses Get-Command (which respects the current PATH) so it works
        after Sync-PathFromRegistry has run. Suppresses PS command-not-found
        suggestions that pollute the console.
    #>
    param([string]$Name)
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-Tool {
    <#
    .SYNOPSIS
        Run "<tool> --version", return @{Ok; Output; FirstLine}.
        Suppresses stderr noise and PS command-not-found suggestions.
    #>
    param([string]$Path)
    try {
        $raw = & $Path --version 2>&1
        $text = ($raw | Out-String).Trim()
        $first = ($text -split "`n")[0].Trim()
        return @{ Ok = ($LASTEXITCODE -eq 0); Output = $text; FirstLine = $first }
    } catch {
        return @{ Ok = $false; Output = ""; FirstLine = "" }
    }
}

# ---------------------------------------------------------------------------
# server.json helpers (mirrors server.py write_server_json / remove_server_json)
# ---------------------------------------------------------------------------

function Write-ServerJson {
    param([int]$Pid_, [int]$Port_, [string]$Host_)
    if (-not (Test-Path $script:GolemDir)) {
        New-Item -ItemType Directory -Path $script:GolemDir -Force | Out-Null
    }
    $payload = @{ pid = $Pid_; port = $Port_; host = $Host_ }
    $json = $payload | ConvertTo-Json -Depth 2
    # UTF-8 no-BOM to match Python json.dumps output exactly
    [System.IO.File]::WriteAllText(
        $script:ServerJsonPath,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-GolemLog "INFO" "Wrote server.json (PID=$Pid_, port=$Port_)"
}

function Remove-ServerJson {
    if (Test-Path $script:ServerJsonPath) {
        try {
            Remove-Item -Path $script:ServerJsonPath -Force -ErrorAction SilentlyContinue
            Write-GolemLog "INFO" "Removed server.json"
        } catch {}
    }
}

function Test-ServerJsonStale {
    <#
    .SYNOPSIS
        If a server.json exists with a dead PID, remove it and return $true.
        If the PID is alive, return $false (server is actually running).
        If no server.json, return $null.
        Mirrors client.py find_server() behavior.
    #>
    if (-not (Test-Path $script:ServerJsonPath)) { return $null }
    try {
        $info = Get-Content $script:ServerJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $pid_ = $info.pid
        $alive = $false
        try {
            $p = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
            if ($p -and -not $p.HasExited) { $alive = $true }
        } catch {}
        if ($alive) {
            return $false  # Server is actually running
        } else {
            Remove-ServerJson
            Write-GolemLog "INFO" "Cleaned stale server.json (dead PID $pid_)"
            return $true   # Was stale, now cleaned
        }
    } catch {
        Remove-ServerJson
        return $true
    }
}

# ---------------------------------------------------------------------------
# Health polling (replaces hardcoded Start-Sleep 1500)
# ---------------------------------------------------------------------------

function Test-ServerHealth {
    param(
        [string]$Host_,
        [int]$Port_,
        [int]$TimeoutSeconds = 15,
        [int]$ExpectedPid = 0,
        [System.Diagnostics.Process]$Process = $null
    )
    $url = "http://$($Host_):$($Port_)/api/server/status"
    $deadline = [DateTime]::Now.AddSeconds($TimeoutSeconds)
    while ([DateTime]::Now -lt $deadline) {
        # Early exit if the process died while we're polling
        if ($Process -and $Process.HasExited) {
            Write-GolemLog "WARN" "Server process exited during health poll (exit=$($Process.ExitCode))"
            return $false
        }
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                try {
                    $data = $resp.Content | ConvertFrom-Json
                    # Verify the responding server is actually ours (not a stale
                    # or foreign process on the same port)
                    if ($ExpectedPid -gt 0 -and $data.pid -and [int]$data.pid -ne $ExpectedPid) {
                        Write-GolemLog "WARN" "Health response PID=$($data.pid) does not match expected PID=$ExpectedPid -- wrong server on port"
                        return $false
                    }
                    Write-GolemLog "INFO" "Server healthy: PID=$($data.pid), uptime=$($data.uptime_seconds)s"
                } catch {}
                return $true
            }
        } catch {
            # Server not ready yet -- keep polling
        }
        Start-Sleep -Milliseconds 300
    }
    Write-GolemLog "WARN" "Server health check timed out after ${TimeoutSeconds}s"
    return $false
}

# ---------------------------------------------------------------------------
# Version and diagnostics helpers
# ---------------------------------------------------------------------------

function Show-GolemVersion {
    try {
        $versionLine = & python -c "from golem.version import get_version_info; i=get_version_info(); print('v' + i['version'] + ' (' + i['architecture'] + ')')" 2>&1
        if ($LASTEXITCODE -eq 0) { return "$versionLine" }
    } catch {}
    return "(version unavailable)"
}

function Show-CrashDiagnostics {
    if (Test-Path $script:LogPath) {
        Write-Host ""
        Write-Host "  --- Last log lines ---" -ForegroundColor DarkGray
        try {
            $lines = Get-Content -Path $script:LogPath -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue
            foreach ($line in $lines) {
                Write-Host "    $line" -ForegroundColor DarkGray
            }
        } catch {}
        Write-Host "  ----------------------" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Environment checks (matches golem doctor order: git, uv, claude, rg, gh)
# ---------------------------------------------------------------------------

function Invoke-SetupChecks {
    Write-Host ""
    Write-Host "  Environment Checks" -ForegroundColor Cyan
    Write-Host "  ------------------" -ForegroundColor DarkGray

    # Refresh PATH from Windows registry so recently-installed tools are found
    # (mirrors src/golem/validator.py _subprocess_env())
    Sync-PathFromRegistry

    # PowerShell version
    $psVer = "$($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor)"
    if ($PSVersionTable.PSVersion.Major -ge 5) {
        Write-Check "OK" "PowerShell $psVer" Green
    } else {
        Write-Check "FAIL" "PowerShell $psVer (need 5.1+)" Red
        return $false
    }

    # Python
    $pyPath = Find-Tool "python"
    if (-not $pyPath) {
        Write-Check "FAIL" "Python not found" Red
        Write-Host "         Install: https://www.python.org/downloads/" -ForegroundColor DarkGray
        return $false
    }
    try {
        $pyInfo = Test-Tool $pyPath
        $pyVer = ($pyInfo.Output -replace '[^0-9.]', '').Trim()
        $pyMajor = [int]($pyVer.Split('.')[0])
        $pyMinor = [int]($pyVer.Split('.')[1])
        if ($pyMajor -ge 3 -and $pyMinor -ge 12) {
            Write-Check "OK" "Python $pyVer" Green
        } else {
            Write-Check "FAIL" "Python $pyVer (need 3.12+)" Red
            return $false
        }
    } catch {
        Write-Check "FAIL" "Python version check failed" Red
        return $false
    }

    # uv
    $uvPath = Find-Tool "uv"
    if (-not $uvPath) {
        Write-Check "FAIL" "uv not found" Red
        Write-Host "         Install: pip install uv  OR  irm https://astral.sh/uv/install.ps1 | iex" -ForegroundColor DarkGray
        return $false
    }
    $uvInfo = Test-Tool $uvPath
    Write-Check "OK" "$($uvInfo.FirstLine)" Green

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
        if ($LASTEXITCODE -ne 0) { throw "import failed" }
        Write-Check "OK" "golem importable" Green
    } catch {
        Write-Check "FAIL" "golem import failed -- try: uv sync" Red
        return $false
    }

    # git (required for worktrees)
    $gitPath = Find-Tool "git"
    if (-not $gitPath) {
        Write-Check "FAIL" "git not found (required for worktrees)" Red
        Write-Host "         Install: winget install Git.Git" -ForegroundColor DarkGray
        return $false
    }
    $gitInfo = Test-Tool $gitPath
    if ($gitInfo.Ok) {
        Write-Check "OK" "$($gitInfo.FirstLine)" Green
    } else {
        Write-Check "FAIL" "git found but --version failed" Red
        return $false
    }

    # claude CLI (required by agents)
    $claudePath = Find-Tool "claude"
    if (-not $claudePath) {
        Write-Check "FAIL" "claude CLI not found (required by agents)" Red
        Write-Host "         Install: npm install -g @anthropic-ai/claude-code" -ForegroundColor DarkGray
        return $false
    }
    $claudeInfo = Test-Tool $claudePath
    if ($claudeInfo.Ok) {
        Write-Check "OK" "claude $($claudeInfo.FirstLine)" Green
    } else {
        # claude --version can exit non-zero in some builds but still print version
        if ($claudeInfo.FirstLine) {
            Write-Check "OK" "claude $($claudeInfo.FirstLine)" Green
        } else {
            Write-Check "FAIL" "claude found at $claudePath but --version failed" Red
            return $false
        }
    }

    # rg / ripgrep (required for spec validation)
    $rgPath = Find-Tool "rg"
    if (-not $rgPath) {
        Write-Check "FAIL" "rg (ripgrep) not found (required for spec validation)" Red
        Write-Host "         Install: winget install BurntSushi.ripgrep  OR  scoop install ripgrep" -ForegroundColor DarkGray
        return $false
    }
    $rgInfo = Test-Tool $rgPath
    if ($rgInfo.Ok) {
        Write-Check "OK" "$($rgInfo.FirstLine)" Green
    } else {
        Write-Check "FAIL" "rg found at $rgPath but --version failed" Red
        return $false
    }

    # gh / GitHub CLI (optional -- needed for PR creation)
    $ghPath = Find-Tool "gh"
    if ($ghPath) {
        $ghInfo = Test-Tool $ghPath
        if ($ghInfo.Ok) {
            Write-Check "OK" "$($ghInfo.FirstLine) (optional)" Green
        } else {
            Write-Check "WARN" "gh found but --version failed (optional, needed for PRs)" Yellow
        }
    } else {
        Write-Check "WARN" "gh (GitHub CLI) not found (optional, needed for PRs)" Yellow
        Write-Host "         Install: winget install GitHub.cli" -ForegroundColor DarkGray
    }

    # Port reclaim — kill stale Golem servers, then verify port is free
    # Clean up any stale server.json first
    $staleResult = Test-ServerJsonStale
    if ($staleResult -eq $false) {
        # server.json has a live PID — a Golem server is already running
        Write-Check "WARN" "Port ${Port} - Golem server already running -- attaching" Yellow
        Write-Host "         Use 'uv run golem server stop' to stop the existing server" -ForegroundColor DarkGray
        $script:ActivePort = $Port
        $script:AttachExisting = $true
        Write-Host ""
        return $true
    } elseif ($staleResult -eq $true) {
        Write-GolemLog "INFO" "Cleaned stale server.json during port check"
    }

    # Attempt to reclaim port by killing stale Golem/uvicorn processes
    $portBusy = $false
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect($HostAddr, $Port)
        $tcp.Close()
        $portBusy = $true
    } catch {
        # Port is free
    }

    if ($portBusy) {
        # Find who is listening on the port
        $reclaimed = $false
        try {
            $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            foreach ($conn in $listeners) {
                $pid_ = $conn.OwningProcess
                try {
                    $proc_ = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
                    if (-not $proc_) { continue }
                    $procName = $proc_.ProcessName.ToLowerInvariant()
                    # Only kill python/uvicorn processes (stale Golem servers)
                    # Never kill system services, wslrelay, etc.
                    if ($procName -in @('python', 'python3', 'pythonw', 'uvicorn')) {
                        Write-Check "WARN" "Killing stale $($proc_.ProcessName) (PID $pid_) on port $Port" Yellow
                        Write-GolemLog "INFO" "Killing stale process PID=$pid_ ($($proc_.ProcessName)) on port $Port"
                        try {
                            $proc_.Kill()
                            $proc_.WaitForExit(3000) | Out-Null
                        } catch {}
                        $reclaimed = $true
                    } else {
                        Write-GolemLog "INFO" "Port $Port occupied by $($proc_.ProcessName) (PID $pid_) -- not a Golem process"
                    }
                } catch {}
            }
        } catch {
            # Get-NetTCPConnection not available (PS 5.1 without NetTCPIP module)
            # Fall back to netstat
            try {
                $netstatLines = & netstat -ano 2>&1 | Select-String ":${Port}\s.*LISTENING"
                foreach ($line in $netstatLines) {
                    $parts = ($line -split '\s+') | Where-Object { $_ }
                    $pid_ = [int]$parts[-1]
                    try {
                        $proc_ = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
                        if (-not $proc_) { continue }
                        $procName = $proc_.ProcessName.ToLowerInvariant()
                        if ($procName -in @('python', 'python3', 'pythonw', 'uvicorn')) {
                            Write-Check "WARN" "Killing stale $($proc_.ProcessName) (PID $pid_) on port $Port" Yellow
                            Write-GolemLog "INFO" "Killing stale process PID=$pid_ ($($proc_.ProcessName)) on port $Port"
                            try {
                                $proc_.Kill()
                                $proc_.WaitForExit(3000) | Out-Null
                            } catch {}
                            $reclaimed = $true
                        }
                    } catch {}
                }
            } catch {}
        }

        if ($reclaimed) {
            # Brief pause for port release, then verify
            Start-Sleep -Milliseconds 500
            $stillBusy = $false
            try {
                $tcp2 = New-Object System.Net.Sockets.TcpClient
                $tcp2.Connect($HostAddr, $Port)
                $tcp2.Close()
                $stillBusy = $true
            } catch {}

            if ($stillBusy) {
                Write-Check "FAIL" "Port $Port still busy after killing stale processes" Red
                Write-Host "         Use: .\Golem.ps1 -Port 9700" -ForegroundColor DarkGray
                return $false
            } else {
                Write-Check "OK" "Port $Port reclaimed" Green
                $script:ActivePort = $Port
            }
        } else {
            # Port busy by a non-Golem process — report what it is and fail
            $blockerInfo = ""
            try {
                $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($conn) {
                    $bp = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
                    if ($bp) { $blockerInfo = "$($bp.ProcessName) (PID $($bp.Id))" }
                }
            } catch {}
            if (-not $blockerInfo) { $blockerInfo = "unknown process" }
            Write-Check "FAIL" "Port $Port blocked by $blockerInfo (not a Golem process)" Red
            Write-Host "         Use: .\Golem.ps1 -Port 9700" -ForegroundColor DarkGray
            return $false
        }
    } else {
        Write-Check "OK" "Port $Port available" Green
        $script:ActivePort = $Port
    }

    # Session state (v2: .golem/sessions/ instead of v1 tasks.json)
    if (Test-Path $script:GolemDir) {
        $sessionsDir = Join-Path $script:GolemDir "sessions"
        if (Test-Path $sessionsDir) {
            $sessionJsonFiles = Get-ChildItem -Path $sessionsDir -Recurse -Filter "session.json" -ErrorAction SilentlyContinue
            $sessionCount = ($sessionJsonFiles | Measure-Object).Count
            $recoveredSessions = @()
            foreach ($f in $sessionJsonFiles) {
                try {
                    $meta = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($meta.status -notin @('archived', 'merged')) {
                        $recoveredSessions += $meta
                    }
                } catch {}
            }
            if ($recoveredSessions.Count -gt 0) {
                Write-Check "WARN" ".golem/ has $($recoveredSessions.Count) recoverable session(s) from prior run" Yellow
                $script:RecoveredSessions = $recoveredSessions
            } elseif ($sessionCount -gt 0) {
                Write-Check "OK" ".golem/ has $sessionCount archived session(s)" Green
            } else {
                Write-Check "OK" ".golem/ exists, no prior sessions" Green
            }
        } else {
            Write-Check "OK" ".golem/ exists, no sessions dir" Green
        }
    } else {
        Write-Check "OK" "Clean start (no .golem/)" Green
    }

    Write-Host ""
    return $true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$script:AttachExisting = $false

Write-Host ""
Write-Host "  ========================================" -ForegroundColor DarkCyan
Write-Host "    GOLEM MULTI-SESSION SERVER" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor DarkCyan

# Handle -Clean switch before setup checks
if ($Clean) {
    if (Test-Path $script:ServerJsonPath) {
        try {
            $existingJson = Get-Content $script:ServerJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $existingPid = $existingJson.pid
            $procAlive = $false
            try {
                $p = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
                if ($p -and -not $p.HasExited) { $procAlive = $true }
            } catch {}
            if ($procAlive) {
                Write-Host ""
                Write-Host "  [FAIL] Server is running (PID $existingPid). Stop it first:" -ForegroundColor Red
                Write-Host "         uv run golem server stop" -ForegroundColor Yellow
                exit 1
            }
        } catch {}
    }
    if (Test-Path $script:GolemDir) {
        Write-Host ""
        Write-Host "  [CLEAN] Wiping .golem/ before start..." -ForegroundColor Yellow
        Write-GolemLog "INFO" "-Clean switch: removing .golem/"
        try {
            Remove-Item -Path $script:GolemDir -Recurse -Force -ErrorAction Stop
            Write-Check "OK" ".golem/ wiped" Green
        } catch {
            Write-Check "FAIL" "Could not wipe .golem/: $_" Red
            exit 1
        }
    } else {
        Write-Check "OK" ".golem/ already absent" Green
    }
    # Note: for full cleanup including golem/* git branches, use: uv run golem clean
}

$setupOk = Invoke-SetupChecks
if (-not $setupOk) {
    Write-Host ""
    Write-Host "  Setup failed. Fix the errors above and try again." -ForegroundColor Red
    Write-GolemLog "FATAL" "Setup checks failed"
    exit 1
}

# If we detected an already-running server, just open the browser and exit
if ($script:AttachExisting) {
    Write-Host "  Opening existing dashboard..." -ForegroundColor Green
    try { Start-Process "http://$($HostAddr):$($script:ActivePort)" } catch {}
    Write-Host ""
    Write-Host "  Attached to existing Golem server on port $($script:ActivePort)." -ForegroundColor Cyan
    Write-GolemLog "INFO" "Attached to existing server on port $($script:ActivePort)"
    exit 0
}

# Post-checks banner with version and URL
$golemVersion = Show-GolemVersion
Write-Host "  ========================================" -ForegroundColor DarkCyan
Write-Host "    GOLEM $golemVersion" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor DarkCyan
Write-Host "  Server:    http://$($HostAddr):$($script:ActivePort)" -ForegroundColor Green
Write-Host "  Dashboard: opens in browser at startup" -ForegroundColor DarkGray
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host "  ========================================" -ForegroundColor DarkCyan

# Show recovered sessions from prior run
if ($script:RecoveredSessions.Count -gt 0) {
    Write-Host ""
    Write-Host "  Recovered sessions from prior run:" -ForegroundColor Yellow
    foreach ($s in $script:RecoveredSessions) {
        $specShort = if ($s.spec_path) { Split-Path $s.spec_path -Leaf } else { "(unknown)" }
        Write-Host "    $($s.id)  [$($s.status)]  $specShort" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-GolemLog "INFO" "Server starting on $($HostAddr):$($script:ActivePort)"

# Auto-restart loop
$crashCount = 0
$crashWindowStart = [DateTime]::Now

try {
    while ($true) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = "python"
        $psi.Arguments        = "-m uvicorn golem.server:create_app --factory --host $HostAddr --port $($script:ActivePort) --log-level warning"
        $psi.WorkingDirectory = $script:ProjectRoot
        $psi.UseShellExecute  = $false
        # Do NOT redirect -- let output flow directly to terminal
        $psi.RedirectStandardOutput = $false
        $psi.RedirectStandardError  = $false
        $psi.CreateNoWindow         = $false
        # Required env vars for golem.server
        $psi.EnvironmentVariables["GOLEM_DIR"]   = $script:GolemDir
        $psi.EnvironmentVariables["GOLEM_PORT"]  = "$($script:ActivePort)"
        $psi.EnvironmentVariables["GOLEM_DEBUG"] = "1"

        try {
            $proc = [System.Diagnostics.Process]::Start($psi)
            $script:ServerProc = $proc
            Write-GolemLog "INFO" "Server started (PID $($proc.Id))"

            # Write server.json for CLI discovery (mirrors cli.py server start)
            Write-ServerJson -Pid_ $proc.Id -Port_ $script:ActivePort -Host_ $HostAddr

            # Poll health endpoint — verify PID matches and detect early-exit
            Write-Host "  Waiting for server..." -ForegroundColor DarkGray -NoNewline
            $healthy = Test-ServerHealth -Host_ $HostAddr -Port_ $script:ActivePort -TimeoutSeconds 15 -ExpectedPid $proc.Id -Process $proc
            if ($proc.HasExited) {
                Write-Host " process died (exit $($proc.ExitCode))" -ForegroundColor Red
                # Fall through to crash handling below
            } elseif ($healthy) {
                Write-Host " ready" -ForegroundColor Green
            } else {
                Write-Host " timeout (may still be starting)" -ForegroundColor Yellow
            }

            # Open browser on first launch only (and only if healthy)
            if ($crashCount -eq 0 -and $healthy -and -not $proc.HasExited) {
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

            # Crash handling with rolling 60s window
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
                Show-CrashDiagnostics
                break
            }

            Write-Host ""
            Write-Host "  Server crashed (exit $exitCode). Restarting... ($crashCount/3)" -ForegroundColor Yellow
            Write-GolemLog "WARN" "Server crashed (exit=$exitCode), restart $crashCount/3"
            Show-CrashDiagnostics
            # Clean up stale server.json before restart
            Remove-ServerJson
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
        Write-GolemLog "INFO" "Server killed (Ctrl+C)"
    }
    # Clean up server.json if lifespan shutdown didn't get to it
    Remove-ServerJson
}

# Session summary on exit
Write-Host ""
$sessionsDir = Join-Path $script:GolemDir "sessions"
if (Test-Path $sessionsDir) {
    $sessionFiles = Get-ChildItem -Path $sessionsDir -Recurse -Filter "session.json" -ErrorAction SilentlyContinue
    if ($sessionFiles) {
        $statusGroups = @{}
        foreach ($f in $sessionFiles) {
            try {
                $meta = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $st = if ($meta.status) { $meta.status } else { "unknown" }
                if (-not $statusGroups.ContainsKey($st)) { $statusGroups[$st] = 0 }
                $statusGroups[$st] = $statusGroups[$st] + 1
            } catch {}
        }
        if ($statusGroups.Count -gt 0) {
            $parts = @()
            foreach ($kv in $statusGroups.GetEnumerator()) {
                $parts += "$($kv.Key): $($kv.Value)"
            }
            Write-Host "  Sessions: $($parts -join ', ')" -ForegroundColor DarkGray
        }
    }
}

Write-Host "  Golem server stopped." -ForegroundColor Cyan
Write-GolemLog "INFO" "Server stopped"
