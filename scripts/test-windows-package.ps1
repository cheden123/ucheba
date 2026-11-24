[CmdletBinding(DefaultParameterSetName='Application')]
param(
    [Parameter(Mandatory, ParameterSetName='Application')][string]$ApplicationDir,
    [Parameter(Mandatory, ParameterSetName='Installer')][string]$Installer
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments, [string]$Log)
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (!$process.WaitForExit(120000)) {
        Stop-Process -InputObject $process -Force
        if ($Log -and (Test-Path -LiteralPath $Log)) { Get-Content -LiteralPath $Log -Tail 40 }
        throw "Истекло время проверки $FilePath."
    }
    if ($process.ExitCode -ne 0) {
        if ($Log -and (Test-Path -LiteralPath $Log)) { Get-Content -LiteralPath $Log -Tail 40 }
        throw "Проверка $FilePath завершилась с кодом $($process.ExitCode)."
    }
}

$env:QT_QPA_PLATFORM = 'offscreen'
$smokeLog = New-TemporaryFile
$env:UCHEBA_SMOKE_LOG = $smokeLog.FullName
if ($Installer) {
    $registryRoot = 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    $appKey = '{9A4F80D8-9AD1-4B56-955B-4F261461413C}_is1'
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        if (Test-Path -LiteralPath (Join-Path $hive "$registryRoot\$appKey")) {
            throw 'Для проверки установщика нужен профиль Windows без существующей установки «Учёба AI».'
        }
    }
    $ApplicationDir = Join-Path ([IO.Path]::GetTempPath()) "ucheba-smoke-$([guid]::NewGuid())"
    $setupLog = New-TemporaryFile
    $arguments = @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/MERGETASKS="!desktopicon"',
        "/DIR=""$ApplicationDir""", "/LOG=""$($setupLog.FullName)"""
    )
    Invoke-CheckedProcess -FilePath $Installer -Arguments $arguments -Log $setupLog.FullName
}
try {
    Invoke-CheckedProcess -FilePath (Join-Path $ApplicationDir 'UchebaAI.exe') -Arguments '--smoke-test' -Log $smokeLog.FullName
    foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
        & (Join-Path $ApplicationDir $tool) -version | Select-Object -First 1
    }
} finally {
    if ($Installer) {
        $uninstaller = Join-Path $ApplicationDir 'unins000.exe'
        $uninstallLog = New-TemporaryFile
        $uninstallArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=""$($uninstallLog.FullName)""")
        Invoke-CheckedProcess -FilePath $uninstaller -Arguments $uninstallArguments -Log $uninstallLog.FullName
        if (Test-Path -LiteralPath (Join-Path $ApplicationDir 'UchebaAI.exe')) {
            throw 'Исполняемый файл остался после удаления.'
        }
    }
}
