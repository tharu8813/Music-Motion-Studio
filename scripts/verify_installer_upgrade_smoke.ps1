#Requires -Version 7.0
<#
.SYNOPSIS
  Pinned previous PUBLIC release -> freshly compiled release acceptance test.
.DESCRIPTION
  Windows CI only; uses a fresh temporary custom installation directory.
  This is an upgrade acceptance test, not Windows 11 consumer or real GPU QA.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PreviousSetupPath,
    [Parameter(Mandatory = $true)][string]$CurrentSetupPath,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+\.\d+

$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true' -or -not $env:RUNNER_TEMP -or -not $env:GITHUB_RUN_ID) {
    throw 'Previous-release upgrade acceptance is restricted to disposable GitHub Actions Windows runners.'
}

$previousSetup = (Resolve-Path -LiteralPath $PreviousSetupPath -ErrorAction Stop).Path
$currentSetup = (Resolve-Path -LiteralPath $CurrentSetupPath -ErrorAction Stop).Path
$runnerRoot = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\', '/')
$runName = "mms-upgrade-$ExpectedPreviousVersion-$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
$installDir = Join-Path $runnerRoot $runName
$logDir = Join-Path $runnerRoot 'mms-installer-acceptance-logs'
$exe = Join-Path $installDir 'Music Motion Studio.exe'

if (Test-Path -LiteralPath $installDir) {
    throw "Refusing to overwrite an existing upgrade test path: $installDir"
}
if (-not $installDir.StartsWith($runnerRoot + [System.IO.Path]::DirectorySeparatorChar,
                                [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Upgrade test target must be inside the disposable runner temporary directory.'
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Invoke-CheckedProcess {
    param([string]$Executable, [string]$Arguments, [string]$Phase, [int]$Seconds = 300)
    Write-Output "$Phase : $Executable $Arguments"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    try {
        if (-not $process.WaitForExit($Seconds * 1000)) {
            try { $process.Kill($true) } catch {}
            throw "$Phase exceeded $Seconds seconds."
        }
        if ($process.ExitCode -ne 0) {
            throw "$Phase failed with exit code $($process.ExitCode)."
        }
    }
    finally { $process.Dispose() }
}

function Invoke-Install {
    param([string]$Setup, [string]$Phase)
    $log = Join-Path $logDir "$Phase.log"
    $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="' +
                 $installDir + '" /LOG="' + $log + '"'
    Invoke-CheckedProcess -Executable $Setup -Arguments $arguments -Phase $Phase
}

Invoke-Install -Setup $previousSetup -Phase 'published-$ExpectedPreviousVersion-install'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'The pinned $ExpectedPreviousVersion release did not install its application launcher.'
}
$previousFileVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($previousFileVersion -ne $ExpectedPreviousVersion) {
    throw "Expected published $ExpectedPreviousVersion launcher; got FileVersion '$previousFileVersion'."
}

# Simulate user-owned artifacts in the custom installation folder and both
# old/new application data locations. These are sentinels, not real projects.
$markers = @(
    (Join-Path $installDir 'ci-user-owned-file.txt'),
    (Join-Path $env:LOCALAPPDATA 'PlaylistCanvas\ci-preserve-upgrade.txt'),
    (Join-Path $env:LOCALAPPDATA 'Music Motion Studio\ci-preserve-upgrade.txt')
)
foreach ($marker in $markers) {
    New-Item -ItemType Directory -Path (Split-Path $marker -Parent) -Force | Out-Null
    [System.IO.File]::WriteAllText($marker, "Retain on upgrade/removal: $runName")
}

Invoke-Install -Setup $currentSetup -Phase 'published-to-current-upgrade'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'Upgrade did not install the current launcher in the custom directory.'
}
$upgradedVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($upgradedVersion -ne $ExpectedCurrentVersion) {
    throw "Upgrade installed FileVersion '$upgradedVersion'; expected '$ExpectedCurrentVersion'."
}
python (Join-Path $PSScriptRoot 'verify_distribution.py') $installDir
if ($LASTEXITCODE -ne 0) { throw 'Upgraded distribution integrity verification failed.' }
Invoke-CheckedProcess -Executable $exe -Arguments '--smoke-test' -Phase 'upgraded-frozen-app-launch' -Seconds 45

foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Upgrade deleted a user-owned marker: $marker"
    }
}
$openCommand = (Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\PlaylistCanvas.Project\shell\open\command' -ErrorAction Stop).GetValue('')
$expectedOpenCommand = '"' + $exe + '" "%1"'
if (-not [string]::Equals($openCommand, $expectedOpenCommand,
                          [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Upgrade left an incorrect .pvsproj association: $openCommand"
}

$uninstallers = @(Get-ChildItem -LiteralPath $installDir -Filter 'unins*.exe' -File)
if ($uninstallers.Count -ne 1) {
    throw "Expected one shared AppId uninstaller after upgrade; got $($uninstallers.Count)."
}
$uninstallLog = Join-Path $logDir 'published-to-current-uninstall.log'
Invoke-CheckedProcess -Executable $uninstallers[0].FullName -Arguments (
    '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + $uninstallLog + '"'
) -Phase 'upgraded-uninstall'
if (Test-Path -LiteralPath $exe) {
    throw 'Upgraded uninstall left the application launcher.'
}
foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Uninstall deleted a user-owned marker: $marker"
    }
}
Write-Output 'PASS: pinned $ExpectedPreviousVersion -> $ExpectedCurrentVersion real Inno Setup upgrade, custom directory, app integrity, smoke, association, user-file preservation and uninstall.'
)][string]$ExpectedPreviousVersion,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+\.\d+

$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true' -or -not $env:RUNNER_TEMP -or -not $env:GITHUB_RUN_ID) {
    throw 'Previous-release upgrade acceptance is restricted to disposable GitHub Actions Windows runners.'
}

$previousSetup = (Resolve-Path -LiteralPath $PreviousSetupPath -ErrorAction Stop).Path
$currentSetup = (Resolve-Path -LiteralPath $CurrentSetupPath -ErrorAction Stop).Path
$runnerRoot = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\', '/')
$runName = "mms-upgrade-$ExpectedPreviousVersion-$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
$installDir = Join-Path $runnerRoot $runName
$logDir = Join-Path $runnerRoot 'mms-installer-acceptance-logs'
$exe = Join-Path $installDir 'Music Motion Studio.exe'

if (Test-Path -LiteralPath $installDir) {
    throw "Refusing to overwrite an existing upgrade test path: $installDir"
}
if (-not $installDir.StartsWith($runnerRoot + [System.IO.Path]::DirectorySeparatorChar,
                                [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Upgrade test target must be inside the disposable runner temporary directory.'
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Invoke-CheckedProcess {
    param([string]$Executable, [string]$Arguments, [string]$Phase, [int]$Seconds = 300)
    Write-Output "$Phase : $Executable $Arguments"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    try {
        if (-not $process.WaitForExit($Seconds * 1000)) {
            try { $process.Kill($true) } catch {}
            throw "$Phase exceeded $Seconds seconds."
        }
        if ($process.ExitCode -ne 0) {
            throw "$Phase failed with exit code $($process.ExitCode)."
        }
    }
    finally { $process.Dispose() }
}

function Invoke-Install {
    param([string]$Setup, [string]$Phase)
    $log = Join-Path $logDir "$Phase.log"
    $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="' +
                 $installDir + '" /LOG="' + $log + '"'
    Invoke-CheckedProcess -Executable $Setup -Arguments $arguments -Phase $Phase
}

Invoke-Install -Setup $previousSetup -Phase 'published-$ExpectedPreviousVersion-install'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'The pinned $ExpectedPreviousVersion release did not install its application launcher.'
}
$previousFileVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($previousFileVersion -ne $ExpectedPreviousVersion) {
    throw "Expected published $ExpectedPreviousVersion launcher; got FileVersion '$previousFileVersion'."
}

# Simulate user-owned artifacts in the custom installation folder and both
# old/new application data locations. These are sentinels, not real projects.
$markers = @(
    (Join-Path $installDir 'ci-user-owned-file.txt'),
    (Join-Path $env:LOCALAPPDATA 'PlaylistCanvas\ci-preserve-upgrade.txt'),
    (Join-Path $env:LOCALAPPDATA 'Music Motion Studio\ci-preserve-upgrade.txt')
)
foreach ($marker in $markers) {
    New-Item -ItemType Directory -Path (Split-Path $marker -Parent) -Force | Out-Null
    [System.IO.File]::WriteAllText($marker, "Retain on upgrade/removal: $runName")
}

Invoke-Install -Setup $currentSetup -Phase 'published-to-current-upgrade'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'Upgrade did not install the current launcher in the custom directory.'
}
$upgradedVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($upgradedVersion -ne $ExpectedCurrentVersion) {
    throw "Upgrade installed FileVersion '$upgradedVersion'; expected '$ExpectedCurrentVersion'."
}
python (Join-Path $PSScriptRoot 'verify_distribution.py') $installDir
if ($LASTEXITCODE -ne 0) { throw 'Upgraded distribution integrity verification failed.' }
Invoke-CheckedProcess -Executable $exe -Arguments '--smoke-test' -Phase 'upgraded-frozen-app-launch' -Seconds 45

foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Upgrade deleted a user-owned marker: $marker"
    }
}
$openCommand = (Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\PlaylistCanvas.Project\shell\open\command' -ErrorAction Stop).GetValue('')
$expectedOpenCommand = '"' + $exe + '" "%1"'
if (-not [string]::Equals($openCommand, $expectedOpenCommand,
                          [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Upgrade left an incorrect .pvsproj association: $openCommand"
}

$uninstallers = @(Get-ChildItem -LiteralPath $installDir -Filter 'unins*.exe' -File)
if ($uninstallers.Count -ne 1) {
    throw "Expected one shared AppId uninstaller after upgrade; got $($uninstallers.Count)."
}
$uninstallLog = Join-Path $logDir 'published-to-current-uninstall.log'
Invoke-CheckedProcess -Executable $uninstallers[0].FullName -Arguments (
    '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + $uninstallLog + '"'
) -Phase 'upgraded-uninstall'
if (Test-Path -LiteralPath $exe) {
    throw 'Upgraded uninstall left the application launcher.'
}
foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Uninstall deleted a user-owned marker: $marker"
    }
}
Write-Output 'PASS: pinned $ExpectedPreviousVersion -> $ExpectedCurrentVersion real Inno Setup upgrade, custom directory, app integrity, smoke, association, user-file preservation and uninstall.'
)][string]$ExpectedCurrentVersion
)

$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true' -or -not $env:RUNNER_TEMP -or -not $env:GITHUB_RUN_ID) {
    throw 'Previous-release upgrade acceptance is restricted to disposable GitHub Actions Windows runners.'
}

$previousSetup = (Resolve-Path -LiteralPath $PreviousSetupPath -ErrorAction Stop).Path
$currentSetup = (Resolve-Path -LiteralPath $CurrentSetupPath -ErrorAction Stop).Path
$runnerRoot = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\', '/')
$runName = "mms-upgrade-$ExpectedPreviousVersion-$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
$installDir = Join-Path $runnerRoot $runName
$logDir = Join-Path $runnerRoot 'mms-installer-acceptance-logs'
$exe = Join-Path $installDir 'Music Motion Studio.exe'

if (Test-Path -LiteralPath $installDir) {
    throw "Refusing to overwrite an existing upgrade test path: $installDir"
}
if (-not $installDir.StartsWith($runnerRoot + [System.IO.Path]::DirectorySeparatorChar,
                                [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Upgrade test target must be inside the disposable runner temporary directory.'
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Invoke-CheckedProcess {
    param([string]$Executable, [string]$Arguments, [string]$Phase, [int]$Seconds = 300)
    Write-Output "$Phase : $Executable $Arguments"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    try {
        if (-not $process.WaitForExit($Seconds * 1000)) {
            try { $process.Kill($true) } catch {}
            throw "$Phase exceeded $Seconds seconds."
        }
        if ($process.ExitCode -ne 0) {
            throw "$Phase failed with exit code $($process.ExitCode)."
        }
    }
    finally { $process.Dispose() }
}

function Invoke-Install {
    param([string]$Setup, [string]$Phase)
    $log = Join-Path $logDir "$Phase.log"
    $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="' +
                 $installDir + '" /LOG="' + $log + '"'
    Invoke-CheckedProcess -Executable $Setup -Arguments $arguments -Phase $Phase
}

Invoke-Install -Setup $previousSetup -Phase 'published-$ExpectedPreviousVersion-install'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'The pinned $ExpectedPreviousVersion release did not install its application launcher.'
}
$previousFileVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($previousFileVersion -ne $ExpectedPreviousVersion) {
    throw "Expected published $ExpectedPreviousVersion launcher; got FileVersion '$previousFileVersion'."
}

# Simulate user-owned artifacts in the custom installation folder and both
# old/new application data locations. These are sentinels, not real projects.
$markers = @(
    (Join-Path $installDir 'ci-user-owned-file.txt'),
    (Join-Path $env:LOCALAPPDATA 'PlaylistCanvas\ci-preserve-upgrade.txt'),
    (Join-Path $env:LOCALAPPDATA 'Music Motion Studio\ci-preserve-upgrade.txt')
)
foreach ($marker in $markers) {
    New-Item -ItemType Directory -Path (Split-Path $marker -Parent) -Force | Out-Null
    [System.IO.File]::WriteAllText($marker, "Retain on upgrade/removal: $runName")
}

Invoke-Install -Setup $currentSetup -Phase 'published-to-current-upgrade'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'Upgrade did not install the current launcher in the custom directory.'
}
$upgradedVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($upgradedVersion -ne $ExpectedCurrentVersion) {
    throw "Upgrade installed FileVersion '$upgradedVersion'; expected '$ExpectedCurrentVersion'."
}
python (Join-Path $PSScriptRoot 'verify_distribution.py') $installDir
if ($LASTEXITCODE -ne 0) { throw 'Upgraded distribution integrity verification failed.' }
Invoke-CheckedProcess -Executable $exe -Arguments '--smoke-test' -Phase 'upgraded-frozen-app-launch' -Seconds 45

foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Upgrade deleted a user-owned marker: $marker"
    }
}
$openCommand = (Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\PlaylistCanvas.Project\shell\open\command' -ErrorAction Stop).GetValue('')
$expectedOpenCommand = '"' + $exe + '" "%1"'
if (-not [string]::Equals($openCommand, $expectedOpenCommand,
                          [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Upgrade left an incorrect .pvsproj association: $openCommand"
}

$uninstallers = @(Get-ChildItem -LiteralPath $installDir -Filter 'unins*.exe' -File)
if ($uninstallers.Count -ne 1) {
    throw "Expected one shared AppId uninstaller after upgrade; got $($uninstallers.Count)."
}
$uninstallLog = Join-Path $logDir 'published-to-current-uninstall.log'
Invoke-CheckedProcess -Executable $uninstallers[0].FullName -Arguments (
    '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + $uninstallLog + '"'
) -Phase 'upgraded-uninstall'
if (Test-Path -LiteralPath $exe) {
    throw 'Upgraded uninstall left the application launcher.'
}
foreach ($marker in $markers) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Uninstall deleted a user-owned marker: $marker"
    }
}
Write-Output 'PASS: pinned $ExpectedPreviousVersion -> $ExpectedCurrentVersion real Inno Setup upgrade, custom directory, app integrity, smoke, association, user-file preservation and uninstall.'
