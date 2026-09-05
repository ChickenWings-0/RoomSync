<#
.SYNOPSIS
    Create a desktop/Start Menu shortcut that opens RoomSync as its own app window.

.DESCRIPTION
    Builds a .lnk pointing at Edge or Chrome in --app= mode, which is what makes
    a local web UI feel like a native application rather than a tab:

      --app=<url>            No address bar, no tabs, no bookmarks. The window
                             gets its own taskbar button and its own icon
                             instead of stacking under the browser's.

      --user-data-dir=<dir>  A DEDICATED browser profile, and the single most
                             important flag here. Without it, --app= reuses the
                             running browser's process and profile, which means:
                             the window inherits your extensions (an aggressive
                             adblocker can and does break a local dashboard),
                             closing the last normal browser window can take the
                             app window with it, and the taskbar groups the app
                             under the browser so the custom icon never shows.
                             With it, the window is genuinely independent.

      --class=<name>         X11-only, and set here purely so the Windows and
                             Linux launchers stay literally identical in shape.
                             Windows ignores it.

    The profile directory is created under %LOCALAPPDATA%\RoomSync\browser-profile,
    beside the app's logs and state -- the same data root utils/paths.py uses,
    so uninstalling is deleting one folder.

.PARAMETER Browser
    edge | chrome | auto. Default auto: prefers Edge on Windows because it is
    always present, falling back to Chrome.

.PARAMETER Url
    The dashboard URL. Defaults to the [web] host/port read from config.toml.

.PARAMETER StartMenu
    Also create a Start Menu entry (so it is searchable and pinnable).

.PARAMETER Uninstall
    Remove the shortcuts and, optionally, the browser profile.

.EXAMPLE
    .\install-shortcut.ps1
    .\install-shortcut.ps1 -Browser chrome -StartMenu
    .\install-shortcut.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [ValidateSet('auto', 'edge', 'chrome')]
    [string] $Browser = 'auto',
    [string] $Url,
    [switch] $StartMenu,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ShortcutName = 'RoomSync.lnk'
$DesktopDir   = [Environment]::GetFolderPath('Desktop')
$StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$ProfileDir   = Join-Path $env:LOCALAPPDATA 'RoomSync\browser-profile'

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    $m" -ForegroundColor Yellow }

# ── Uninstall ────────────────────────────────────────────────────────
if ($Uninstall) {
    Write-Step 'Removing shortcuts'
    foreach ($dir in @($DesktopDir, $StartMenuDir)) {
        $path = Join-Path $dir $ShortcutName
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Ok "Removed $path"
        }
    }
    if (Test-Path $ProfileDir) {
        Write-Host ''
        Write-Warn "The browser profile is still at: $ProfileDir"
        Write-Warn 'Delete it manually if you want the window state gone too.'
    }
    Write-Host "`nDone." -ForegroundColor Green
    return
}

# ── Resolve the URL from config.toml ─────────────────────────────────
# Read rather than hard-coded: someone who moved the app off 8420 should not
# get a shortcut to a dead port. Parsed with a regex over the [web] section
# because Windows PowerShell 5.1 has no TOML reader and taking a dependency for
# two integers would be silly.
if (-not $Url) {
    $configPath = Join-Path $ProjectRoot 'config.toml'
    $webHost = '127.0.0.1'
    $webPort = 8420
    if (Test-Path $configPath) {
        $toml = Get-Content $configPath -Raw
        if ($toml -match '(?ms)^\[web\](.*?)(?=^\[|\Z)') {
            $section = $Matches[1]
            if ($section -match 'host\s*=\s*"([^"]+)"') { $webHost = $Matches[1] }
            if ($section -match 'port\s*=\s*(\d+)')     { $webPort = [int]$Matches[1] }
        }
    }
    # 0.0.0.0 is a bind address, not something a browser can navigate to.
    if ($webHost -in @('0.0.0.0', '')) { $webHost = '127.0.0.1' }
    $Url = "http://${webHost}:${webPort}"
}
Write-Step "Dashboard URL: $Url"

# ── Locate the browser ───────────────────────────────────────────────
Write-Step 'Locating a Chromium-based browser'

$edgePaths = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

function Find-First { param([string[]] $Paths)
    $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

$BrowserExe = $null
switch ($Browser) {
    'edge'   { $BrowserExe = Find-First $edgePaths }
    'chrome' { $BrowserExe = Find-First $chromePaths }
    'auto'   {
        # Edge first: it ships with Windows, so it is the one that is
        # guaranteed to be there. Both are Chromium and --app behaves
        # identically on each.
        $BrowserExe = Find-First $edgePaths
        if (-not $BrowserExe) { $BrowserExe = Find-First $chromePaths }
    }
}

if (-not $BrowserExe) {
    throw ("No Chromium-based browser was found. Install Microsoft Edge or " +
           "Google Chrome, or pass -Browser with the one you have.")
}
Write-Ok "Using: $BrowserExe"

# ── Icon ─────────────────────────────────────────────────────────────
Write-Step 'Resolving the icon'
$IconPath = Join-Path $ProjectRoot 'web\static\icons\roomsync.ico'
if (-not (Test-Path $IconPath)) {
    Write-Warn 'roomsync.ico not found -- generating the icon set now.'
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source (Join-Path $ProjectRoot 'scripts\make_icons.py') | Out-Null
    }
}
if (Test-Path $IconPath) {
    Write-Ok "Icon: $IconPath"
} else {
    # The shortcut is still perfectly usable; it just inherits the browser's
    # icon, which is exactly the thing --user-data-dir was meant to avoid.
    Write-Warn 'No icon available; the shortcut will use the browser icon.'
    Write-Warn 'Run: python scripts\make_icons.py'
    $IconPath = $BrowserExe
}

# ── Build the shortcut ───────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

# --no-first-run / --no-default-browser-check: a fresh profile otherwise opens
# the welcome flow and a "make me your default browser" prompt on first launch,
# in what is supposed to be a single-purpose app window.
$Arguments = @(
    "--app=$Url"
    "--user-data-dir=`"$ProfileDir`""
    '--class=RoomSync'
    '--no-first-run'
    '--no-default-browser-check'
) -join ' '

function New-AppShortcut {
    param([string] $Path)

    $shell = New-Object -ComObject WScript.Shell
    try {
        $lnk = $shell.CreateShortcut($Path)
        $lnk.TargetPath       = $BrowserExe
        $lnk.Arguments        = $Arguments
        $lnk.WorkingDirectory = Split-Path $BrowserExe -Parent
        $lnk.IconLocation     = "$IconPath,0"
        $lnk.Description      = 'RoomSync lighting dashboard'
        $lnk.WindowStyle      = 1        # normal window
        $lnk.Save()
    } finally {
        # The COM object holds a handle to the shell; released explicitly so
        # the script does not leave one behind in a long-running session.
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

Write-Step 'Creating the shortcut'
$desktopLnk = Join-Path $DesktopDir $ShortcutName
New-AppShortcut -Path $desktopLnk
Write-Ok "Desktop: $desktopLnk"

if ($StartMenu) {
    $startLnk = Join-Path $StartMenuDir $ShortcutName
    New-AppShortcut -Path $startLnk
    Write-Ok "Start Menu: $startLnk"
}

# ── Summary ──────────────────────────────────────────────────────────
Write-Host ''
Write-Host 'Shortcut installed.' -ForegroundColor Green
Write-Host ''
Write-Host "  Target:   $BrowserExe"
Write-Host "  Args:     $Arguments"
Write-Host "  Profile:  $ProfileDir"
Write-Host ''
Write-Host 'The window has no address bar and its own taskbar icon. Pin it from'
Write-Host 'the taskbar to keep it there.'
Write-Host ''
Write-Host 'RoomSync itself must be running for the window to load anything --'
Write-Host 'see install-autostart.ps1 to start it at logon.'
