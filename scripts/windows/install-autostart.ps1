<#
.SYNOPSIS
    Register RoomSync (and OpenRGB) to start automatically at logon.

.DESCRIPTION
    Creates two Scheduled Tasks, because the two processes have genuinely
    different requirements and one task cannot express both:

      RoomSync OpenRGB Server   Runs with HIGHEST privileges. OpenRGB drives
                                motherboard and RAM RGB through kernel-level
                                SMBus access, and without elevation it starts
                                but enumerates none of those devices -- the
                                server answers on 6742 and reports zero
                                controllers, which looks exactly like a
                                RoomSync bug and is not one.

      RoomSync                  Runs as YOU, unelevated, delayed 20 seconds.
                                Two reasons for the delay, and they compound:
                                the OpenRGB server needs a few seconds to bind
                                its port and enumerate hardware, and the
                                Bluetooth stack is frequently not ready at the
                                instant the shell comes up -- connecting to
                                three strips against a half-initialised adapter
                                is how you get a boot where all three fail and
                                sit in reconnect backoff.

    Deliberately NOT elevated for RoomSync itself. It needs no privilege it
    does not already have as the logged-in user, and an elevated process cannot
    be interacted with from an unelevated shell -- which would break the tray
    icon's interaction with the desktop session.

    Scheduled Tasks rather than a Startup-folder shortcut or a Run key: only
    the task scheduler can express "delay after logon", "restart if it dies",
    and "run elevated without a UAC prompt". A Run key can do none of the three.

.PARAMETER OpenRgbPath
    Path to OpenRGB.exe. Auto-detected from the usual install locations.

.PARAMETER Delay
    Seconds to wait after logon before starting RoomSync. Default 20.

.PARAMETER SkipOpenRgb
    Only register the RoomSync task. For a machine with no OpenRGB devices.

.PARAMETER Uninstall
    Remove both tasks.

.EXAMPLE
    .\install-autostart.ps1
    .\install-autostart.ps1 -OpenRgbPath "D:\Tools\OpenRGB\OpenRGB.exe"
    .\install-autostart.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $OpenRgbPath,
    [int]    $Delay = 20,
    [switch] $SkipOpenRgb,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$TASK_ROOMSYNC = 'RoomSync'
$TASK_OPENRGB  = 'RoomSync OpenRGB Server'

# The project root is this script's grandparent: scripts\windows\ -> repo root.
# Resolved from $PSScriptRoot rather than the working directory, so the script
# works when invoked by full path from anywhere.
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    $m" -ForegroundColor Yellow }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ── Uninstall ────────────────────────────────────────────────────────
if ($Uninstall) {
    Write-Step 'Removing scheduled tasks'
    foreach ($name in @($TASK_ROOMSYNC, $TASK_OPENRGB)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Ok "Removed '$name'"
        } else {
            Write-Warn "'$name' was not registered"
        }
    }
    Write-Host "`nDone. RoomSync will no longer start at logon." -ForegroundColor Green
    return
}

# Registering an elevated (RunLevel Highest) task requires elevation itself.
# Checked up front rather than failing halfway with two tasks in an
# inconsistent state.
if (-not $SkipOpenRgb -and -not (Test-Admin)) {
    throw ("Administrator rights are required to register the elevated OpenRGB " +
           "task. Re-run this from an elevated PowerShell, or pass -SkipOpenRgb " +
           "to install only the RoomSync task.")
}

# ── Locate pythonw.exe ───────────────────────────────────────────────
# pythonw, NOT python: pythonw.exe is the GUI subsystem build and opens no
# console window. With plain python.exe a console flashes at every logon and,
# worse, stays in the taskbar for the life of the app -- which defeats the
# entire point of the tray icon.
Write-Step 'Locating the Python interpreter'

$venvPythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
if (Test-Path $venvPythonw) {
    $Pythonw = $venvPythonw
    Write-Ok "Using the project virtualenv: $Pythonw"
} else {
    $py = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $py) {
        throw ("pythonw.exe was not found on PATH and there is no .venv in " +
               "$ProjectRoot. Install Python, or create the virtualenv first.")
    }
    $Pythonw = $py.Source
    Write-Ok "Using pythonw from PATH: $Pythonw"
}

$MainPy = Join-Path $ProjectRoot 'main.py'
if (-not (Test-Path $MainPy)) { throw "main.py not found at $MainPy" }

# ── Locate OpenRGB ───────────────────────────────────────────────────
if (-not $SkipOpenRgb) {
    Write-Step 'Locating OpenRGB'
    if (-not $OpenRgbPath) {
        $candidates = @(
            "$env:ProgramFiles\OpenRGB\OpenRGB.exe",
            "${env:ProgramFiles(x86)}\OpenRGB\OpenRGB.exe",
            "$env:LOCALAPPDATA\OpenRGB\OpenRGB.exe",
            "$env:ProgramFiles\OpenRGB Windows 64-bit\OpenRGB.exe"
        )
        $OpenRgbPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $OpenRgbPath -or -not (Test-Path $OpenRgbPath)) {
        Write-Warn 'OpenRGB.exe was not found. Skipping its task.'
        Write-Warn 'Pass -OpenRgbPath "C:\path\to\OpenRGB.exe" to register it.'
        $SkipOpenRgb = $true
    } else {
        Write-Ok "Found: $OpenRgbPath"
    }
}

# ── Task 1: OpenRGB, elevated ────────────────────────────────────────
if (-not $SkipOpenRgb) {
    Write-Step "Registering '$TASK_OPENRGB'"

    # --server starts the SDK listener RoomSync connects to on 6742.
    # --startminimized keeps its window out of the way; it is a background
    # service here, not something anyone wants to look at.
    $action = New-ScheduledTaskAction -Execute $OpenRgbPath `
        -Argument '--server --startminimized' `
        -WorkingDirectory (Split-Path $OpenRgbPath -Parent)

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    # RunLevel Highest is the whole reason this is a separate task. See the
    # .DESCRIPTION -- unelevated OpenRGB enumerates no SMBus devices.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest

    # ExecutionTimeLimit 0 = never kill it. The default is three days, after
    # which the task scheduler would terminate a perfectly healthy server on a
    # machine that simply had not been rebooted.
    #
    # DisallowHardTerminate / AllowStartIfOnBatteries: this is a desktop RGB
    # daemon, none of the power-saving defaults are appropriate.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TASK_OPENRGB -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description 'OpenRGB SDK server for RoomSync. Elevated for SMBus device access.' `
        -Force | Out-Null

    Write-Ok "Registered (elevated, at logon)"
}

# ── Task 2: RoomSync, delayed ────────────────────────────────────────
Write-Step "Registering '$TASK_ROOMSYNC'"

# -WorkingDirectory is belt-and-braces now that utils/paths.py resolves
# everything absolutely, but it keeps relative paths in any future subprocess
# honest and costs nothing.
$action = New-ScheduledTaskAction -Execute $Pythonw `
    -Argument "`"$MainPy`"" -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${Delay}S"   # ISO 8601 duration; the cmdlet has no -Delay

# Limited, not Highest: RoomSync needs nothing beyond what the user has, and an
# elevated process cannot interact with an unelevated desktop session -- which
# is what the tray icon lives in.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

# RestartCount is the supervisor of last resort: the app already self-heals
# BLE, OpenRGB and its own tasks, so a full process exit means something we did
# not anticipate, and coming back is better than staying dead until next logon.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TASK_ROOMSYNC -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "RoomSync lighting engine. Delayed ${Delay}s so OpenRGB and the Bluetooth stack are ready." `
    -Force | Out-Null

Write-Ok "Registered (at logon + ${Delay}s delay)"

# ── Summary ──────────────────────────────────────────────────────────
Write-Host ''
Write-Host 'Autostart installed.' -ForegroundColor Green
Write-Host ''
Write-Host '  Start now:     Start-ScheduledTask -TaskName ''RoomSync'''
Write-Host '  Check status:  Get-ScheduledTask -TaskName ''RoomSync*'' | Get-ScheduledTaskInfo'
Write-Host '  Stop:          Stop-ScheduledTask -TaskName ''RoomSync'''
Write-Host '  Remove:        .\install-autostart.ps1 -Uninstall'
Write-Host ''
Write-Host '  Logs:          ' -NoNewline
Write-Host (Join-Path $env:LOCALAPPDATA 'RoomSync\logs\roomsync.log')
Write-Host ''
Write-Host 'The app has no console window (pythonw). Use the tray icon to quit it,'
Write-Host 'and the log file above for anything that goes wrong at startup.'
