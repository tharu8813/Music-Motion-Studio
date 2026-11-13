#Requires -Version 7.0
<#
.SYNOPSIS
  Disposable Windows CI acceptance for the REAL Inno Setup package.
.DESCRIPTION
  Installs twice, verifies packaged integrity and smoke launch, and uninstalls.
  Intentionally refuses non-CI environments to avoid touching a user installation.
  Real Windows 11 user/upgrade/GPU export testing remains a separate release gate.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SetupPath
)

$ErrorActionPreference = 'Stop'

if ($env:CI -ne 'true' -or -not $env:RUNNER_TEMP -or -not $env:GITHUB_RUN_ID) {
    throw 'This installer acceptance script is restricted to disposable GitHub Actions Windows runners.'
}

$setup = (Resolve-Path -LiteralPath $SetupPath -ErrorAction Stop).Path
$runnerRoot = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\', '/')
$runName = "mms-installer-$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
$installDir = Join-Path $runnerRoot $runName
$logDir = Join-Path $runnerRoot 'mms-installer-acceptance-logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

if (Test-Path -LiteralPath $installDir) {
    throw "Refusing to overwrite an existing installation test path: $installDir"
}
if (-not $installDir.StartsWith($runnerRoot + [System.IO.Path]::DirectorySeparatorChar,
                               [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installer test target must be inside the GitHub Actions temporary directory.'
}

# Every setup and uninstall action has a deadline. The Windows hosted runner
# is disposable, and no production user installation is ever selected.
function Invoke-CheckedProcess {
    param([string]$Executable, [string]$Arguments, [string]$Description, [int]$Seconds = 240)
    Write-Output "$Description : $Executable $Arguments"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    try {
        if (-not $process.WaitForExit($Seconds * 1000)) {
            try { $process.Kill($true) } catch {}
            throw "$Description exceeded $Seconds seconds."
        }
        if ($process.ExitCode -ne 0) {
            throw "$Description failed with exit code $($process.ExitCode)."
        }
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-Setup {
    param([string]$Phase)
    $log = Join-Path $logDir "$Phase.log"
    $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="' +
                 $installDir + '" /LOG="' + $log + '"'
    Invoke-CheckedProcess -Executable $setup -Arguments $arguments -Description $Phase -Seconds 300
}

function Confirm-Installed {
    param([string]$Phase)
    $executable = Join-Path $installDir 'Music Motion Studio.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "$Phase did not install the application executable."
    }
    $manifest = Join-Path $installDir 'integrity.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "$Phase did not install integrity.json."
    }
    python (Join-Path $PSScriptRoot 'verify_distribution.py') $installDir
    if ($LASTEXITCODE -ne 0) { throw "$Phase installed an incomplete or corrupted distribution." }
    Invoke-CheckedProcess -Executable $executable -Arguments '--smoke-test' -Description "$Phase frozen-app launch" -Seconds 45
}

Invoke-Setup -Phase 'first-install'
Confirm-Installed -Phase 'first-install'

# Inno must not delete unowned files in the app folder or user data.
$unowned = Join-Path $installDir 'ci-user-owned-file.txt'
[System.IO.File]::WriteAllText($unowned, 'Do not delete this during upgrades or removal.')
$userData = Join-Path $env:LOCALAPPDATA 'PlaylistCanvas'
New-Item -ItemType Directory -Path $userData -Force | Out-Null
$userMarker = Join-Path $userData "ci-preserve-$runName.txt"
[System.IO.File]::WriteAllText($userMarker, 'User data must survive reinstall/uninstall.')

Invoke-Setup -Phase 'in-place-reinstall'
Confirm-Installed -Phase 'in-place-reinstall'

if (-not (Test-Path -LiteralPath $unowned) -or -not (Test-Path -LiteralPath $userMarker)) {
    throw 'In-place reinstall removed user-owned files.'
}

# File-association registration is a required installer contract.
$association = Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\.pvsproj' -ErrorAction Stop
if ($association.GetValue('') -ne 'PlaylistCanvas.Project') {
    throw 'The .pvsproj file association is not registered to the stable ProgID.'
}
$openCommand = (Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\PlaylistCanvas.Project\shell\open\command' -ErrorAction Stop).GetValue('')
if (-not $openCommand.Contains('Music Motion Studio.exe') -or -not $openCommand.Contains('%1')) {
    throw 'The .pvsproj open command is missing the application or file argument.'
}

$uninstaller = Get-ChildItem -LiteralPath $installDir -Filter 'unins*.exe' -File |
    Sort-Object Name | Select-Object -First 1
if (-not $uninstaller) { throw 'Inno Setup uninstaller is missing.' }
$uninstallLog = Join-Path $logDir 'uninstall.log'
$arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + $uninstallLog + '"'
Invoke-CheckedProcess -Executable $uninstaller.FullName -Arguments $arguments -Description 'uninstall' -Seconds 300

if (Test-Path -LiteralPath (Join-Path $installDir 'Music Motion Studio.exe')) {
    throw 'Uninstallation left the installed application executable behind.'
}
if (-not (Test-Path -LiteralPath $unowned) -or -not (Test-Path -LiteralPath $userMarker)) {
    throw 'Uninstallation removed user-owned files.'
}
Write-Output 'PASS: installer clean install, same-version reinstall, frozen smoke, association and user-data-preserving uninstall.'
