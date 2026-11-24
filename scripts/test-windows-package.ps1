[CmdletBinding(DefaultParameterSetName='Application')]
param(
    [Parameter(Mandatory, ParameterSetName='Application')][string]$ApplicationDir,
    [Parameter(Mandatory, ParameterSetName='Installer')][string]$Installer
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments)
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (!$process.WaitForExit(120000)) {
        Stop-Process -InputObject $process -Force
        throw "Истекло время проверки $FilePath."
    }
    if ($process.ExitCode -ne 0) {
        throw "Проверка $FilePath завершилась с кодом $($process.ExitCode)."
    }
}

$env:QT_QPA_PLATFORM = 'offscreen'
if ($Installer) {
    $registryRoot = 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    $appKey = '{9A4F80D8-9AD1-4B56-955B-4F261461413C}_is1'
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        if (Test-Path -LiteralPath (Join-Path $hive "$registryRoot\$appKey")) {
            throw 'Для проверки установщика нужен профиль Windows без существующей установки «Учёба AI».'
        }
    }
    $ApplicationDir = Join-Path ([IO.Path]::GetTempPath()) "ucheba-smoke-$([guid]::NewGuid())"
    $arguments = @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/MERGETASKS="!desktopicon"',
        "/DIR=""$ApplicationDir"""
    )
    Invoke-CheckedProcess -FilePath $Installer -Arguments $arguments
}
try {
    Invoke-CheckedProcess -FilePath (Join-Path $ApplicationDir 'UchebaAI.exe') -Arguments '--smoke-test'
    foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
        & (Join-Path $ApplicationDir $tool) -version | Select-Object -First 1
    }
} finally {
    if ($Installer) {
        $uninstaller = Join-Path $ApplicationDir 'unins000.exe'
        Invoke-CheckedProcess -FilePath $uninstaller -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        if (Test-Path -LiteralPath (Join-Path $ApplicationDir 'UchebaAI.exe')) {
            throw 'Исполняемый файл остался после удаления.'
        }
    }
}
