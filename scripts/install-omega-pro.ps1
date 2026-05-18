#Requires -Version 5.1
<#
.SYNOPSIS
    OMEGA Pro installer for Windows + Claude Desktop.
    Right-click -> "Run with PowerShell" or execute from a terminal.

.DESCRIPTION
    1. Finds or installs Python 3.11+
    2. Installs uv (fast Python package manager) if not present
    3. Creates an isolated venv at %LOCALAPPDATA%\OMEGA\venv via uv
    4. Installs omega-memory (core) from PyPI via uv
    5. Finds and installs the Pro wheel from Downloads/Desktop
    6. Runs omega setup (creates ~/.omega/, downloads ONNX model)
    7. Configures Claude Desktop MCP (claude_desktop_config.json)
    8. Activates license key
    9. Opens the Pro dashboard in a browser

    Uses uv for 10-100x faster installs and hermetic dependency resolution.
    Falls back to pip if uv is unavailable.
    Safe to run multiple times (idempotent).
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ─── Helpers ───────────────────────────────────────────────────────────

function Write-OK    ($msg) { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-FAIL  ($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-WARN  ($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Step  ($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

function Exit-WithError ($msg) {
    Write-FAIL $msg
    Write-Host "`nPress any key to exit..." -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

# ─── Paths ─────────────────────────────────────────────────────────────

$OmegaRoot     = Join-Path $env:LOCALAPPDATA "OMEGA"
$VenvDir       = Join-Path $OmegaRoot "venv"
$VenvPython    = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip       = Join-Path $VenvDir "Scripts\pip.exe"
$OmegaHome     = Join-Path $env:USERPROFILE ".omega"
$ClaudeAppData = Join-Path $env:APPDATA "Claude"
$ClaudeConfig  = Join-Path $ClaudeAppData "claude_desktop_config.json"

# ─── Banner ────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor DarkYellow
Write-Host "  ║       OMEGA Pro  —  Windows Setup     ║" -ForegroundColor DarkYellow
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor DarkYellow
Write-Host ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 1: Find Python 3.11+
# ═══════════════════════════════════════════════════════════════════════

Write-Step 1 "Finding Python 3.11+"

$PythonExe = $null

foreach ($candidate in @("py", "python3", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python\s+(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) {
                $PythonExe = $candidate
                Write-OK "Found $ver ($candidate)"
                break
            } else {
                Write-WARN "$candidate is $ver (need 3.11+), skipping"
            }
        }
    } catch {
        # candidate not found, continue
    }
}

if (-not $PythonExe) {
    Write-WARN "No Python 3.11+ found. Downloading Python 3.12 installer..."
    $installerUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
    $installerPath = Join-Path $env:TEMP "python-3.12.8-amd64.exe"

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
        Write-OK "Downloaded Python installer"
    } catch {
        Exit-WithError "Failed to download Python: $_"
    }

    Write-Host "  Installing Python 3.12 (user-level, no admin required)..." -ForegroundColor Gray
    Start-Process -Wait -FilePath $installerPath -ArgumentList `
        "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "/quiet"

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + `
                [System.Environment]::GetEnvironmentVariable("Path", "Machine")

    foreach ($candidate in @("py", "python3", "python")) {
        try {
            $ver = & $candidate --version 2>&1
            if ($ver -match "Python\s+3\.(1[1-9]|[2-9]\d)") {
                $PythonExe = $candidate
                Write-OK "Installed $ver"
                break
            }
        } catch { }
    }

    if (-not $PythonExe) {
        Exit-WithError "Python install succeeded but 'python' not found in PATH. Close and reopen PowerShell, then run this script again."
    }
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 2: Install uv (fast Python package manager)
# ═══════════════════════════════════════════════════════════════════════

Write-Step 2 "Setting up package manager"

$UseUv = $false
$UvExe = $null

# Check if uv is already installed
$UvExe = Get-Command "uv" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if ($UvExe) {
    Write-OK "uv already installed ($UvExe)"
    $UseUv = $true
} else {
    Write-Host "  Installing uv (fast Python package manager)..." -ForegroundColor Gray
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-Expression "& { $(Invoke-WebRequest -UseBasicParsing https://astral.sh/uv/install.ps1) }"
        # Refresh PATH to find uv
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + `
                    [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        $UvExe = Get-Command "uv" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
        if ($UvExe) {
            Write-OK "uv installed ($UvExe)"
            $UseUv = $true
        } else {
            Write-WARN "uv installed but not found in PATH. Falling back to pip."
        }
    } catch {
        Write-WARN "Could not install uv: $_. Falling back to pip."
    }
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 3: Create venv and install packages
# ═══════════════════════════════════════════════════════════════════════

Write-Step 3 "Creating virtual environment and installing packages"

if (-not (Test-Path $OmegaRoot)) {
    New-Item -ItemType Directory -Path $OmegaRoot -Force | Out-Null
}

function Find-ProWheel {
    $searchDirs = @(
        (Join-Path $env:USERPROFILE "Downloads"),
        (Join-Path $env:USERPROFILE "Desktop"),
        $env:USERPROFILE
    )
    foreach ($dir in $searchDirs) {
        if (Test-Path $dir) {
            $wheel = Get-ChildItem -Path $dir -Filter "omega_memory_pro-*.whl" -ErrorAction SilentlyContinue |
                     Sort-Object LastWriteTime -Descending |
                     Select-Object -First 1
            if ($wheel) { return $wheel.FullName }
        }
    }
    return $null
}

if ($UseUv) {
    # --- uv path: hermetic venv + install ---
    if (-not (Test-Path $VenvPython)) {
        try {
            & $UvExe venv $VenvDir --python $PythonExe --quiet
            Write-OK "Created venv at $VenvDir"
        } catch {
            Exit-WithError "Failed to create venv: $_"
        }
    } else {
        Write-OK "Venv already exists at $VenvDir"
    }

    # Install core
    try {
        & $UvExe pip install omega-memory --python $VenvPython --quiet
        Write-OK "omega-memory installed (via uv)"
    } catch {
        Exit-WithError "Failed to install omega-memory: $_"
    }

    # Find and install Pro wheel
    Write-Host "  Looking for Pro wheel..." -ForegroundColor Gray
    $proWheel = Find-ProWheel
    if (-not $proWheel) {
        Write-WARN "Pro wheel not found in Downloads or Desktop."
        Write-Host "  Please download it from https://admin.omegamax.co/pro/dashboard" -ForegroundColor Gray
        Write-Host "  then press any key to continue..." -ForegroundColor Yellow
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        $proWheel = Find-ProWheel
    }
    if (-not $proWheel) {
        Exit-WithError "Could not find omega_memory_pro-*.whl. Download it from the Pro dashboard and try again."
    }
    Write-Host "  Found: $proWheel" -ForegroundColor Gray
    try {
        & $UvExe pip install $proWheel --python $VenvPython --quiet
        Write-OK "Pro wheel installed (via uv)"
    } catch {
        Exit-WithError "Failed to install Pro wheel: $_"
    }
} else {
    # --- pip fallback ---
    if (-not (Test-Path $VenvPython)) {
        try {
            & $PythonExe -m venv $VenvDir
            Write-OK "Created venv at $VenvDir"
        } catch {
            Exit-WithError "Failed to create venv: $_"
        }
    } else {
        Write-OK "Venv already exists at $VenvDir"
    }

    # Upgrade pip
    & $VenvPython -m pip install --upgrade pip --quiet 2>$null

    # Install core
    try {
        & $VenvPip install omega-memory --quiet
        Write-OK "omega-memory installed (via pip)"
    } catch {
        Exit-WithError "Failed to install omega-memory: $_"
    }

    # Find and install Pro wheel
    Write-Host "  Looking for Pro wheel..." -ForegroundColor Gray
    $proWheel = Find-ProWheel
    if (-not $proWheel) {
        Write-WARN "Pro wheel not found in Downloads or Desktop."
        Write-Host "  Please download it from https://admin.omegamax.co/pro/dashboard" -ForegroundColor Gray
        Write-Host "  then press any key to continue..." -ForegroundColor Yellow
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        $proWheel = Find-ProWheel
    }
    if (-not $proWheel) {
        Exit-WithError "Could not find omega_memory_pro-*.whl. Download it from the Pro dashboard and try again."
    }
    Write-Host "  Found: $proWheel" -ForegroundColor Gray
    try {
        & $VenvPip install $proWheel --quiet
        Write-OK "Pro wheel installed (via pip)"
    } catch {
        Exit-WithError "Failed to install Pro wheel: $_"
    }
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 4: Run omega setup
# ═══════════════════════════════════════════════════════════════════════

Write-Step 4 "Running omega setup"

try {
    & $VenvPython -m omega setup --client claude-desktop 2>&1 | ForEach-Object { Write-Host "  $_" }
    Write-OK "omega setup complete"
} catch {
    Write-WARN "omega setup had warnings (this may be OK): $_"
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 5: Configure Claude Desktop MCP
# ═══════════════════════════════════════════════════════════════════════

Write-Step 5 "Configuring Claude Desktop"

if (-not (Test-Path $ClaudeAppData)) {
    New-Item -ItemType Directory -Path $ClaudeAppData -Force | Out-Null
}

# Back up existing config
if (Test-Path $ClaudeConfig) {
    $backupPath = "$ClaudeConfig.backup.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item $ClaudeConfig $backupPath
    Write-OK "Backed up existing config to $backupPath"
}

# Read or create config
$config = @{}
if (Test-Path $ClaudeConfig) {
    try {
        $config = Get-Content $ClaudeConfig -Raw | ConvertFrom-Json -AsHashtable
    } catch {
        Write-WARN "Could not parse existing config, creating new one"
        $config = @{}
    }
}

if (-not $config.ContainsKey("mcpServers")) {
    $config["mcpServers"] = @{}
}

# Normalize the venv python path for JSON (forward slashes)
$pythonPathNormalized = $VenvPython.Replace("\", "/")

$config["mcpServers"]["omega-memory"] = @{
    "command" = $pythonPathNormalized
    "args"    = @("-m", "omega.server.mcp_server")
}

$configJson = $config | ConvertTo-Json -Depth 10
Set-Content -Path $ClaudeConfig -Value $configJson -Encoding UTF8
Write-OK "MCP server registered in Claude Desktop config"

# ═══════════════════════════════════════════════════════════════════════
# STEP 6: Activate license
# ═══════════════════════════════════════════════════════════════════════

Write-Step 6 "License activation"

$licenseKey = Read-Host "  Enter your Pro license key"

if ([string]::IsNullOrWhiteSpace($licenseKey)) {
    Write-WARN "No license key entered. You can activate later with: omega activate <key>"
} else {
    try {
        & $VenvPython -m omega activate $licenseKey 2>&1 | ForEach-Object { Write-Host "  $_" }
        Write-OK "License activated"
    } catch {
        Write-WARN "License activation issue: $_"
        Write-Host "  You can try again later with: & '$VenvPython' -m omega activate $licenseKey" -ForegroundColor Gray
    }
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 7: Open dashboard
# ═══════════════════════════════════════════════════════════════════════

Write-Step 7 "Opening Pro dashboard"

Start-Process "https://admin.omegamax.co/pro/dashboard"

# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║         Setup Complete!               ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Installed to:" -ForegroundColor Gray
Write-Host "    Venv:     $VenvDir" -ForegroundColor Gray
Write-Host "    OMEGA:    $OmegaHome" -ForegroundColor Gray
Write-Host "    MCP cfg:  $ClaudeConfig" -ForegroundColor Gray
Write-Host ""
Write-Host "  NEXT STEP: Restart Claude Desktop (close and reopen)" -ForegroundColor Yellow
Write-Host "  Then ask Claude: 'What OMEGA tools are available?'" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Press any key to exit..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
