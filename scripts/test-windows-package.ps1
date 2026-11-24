[CmdletBinding(DefaultParameterSetName='Application')]
param(
    [Parameter(Mandatory, ParameterSetName='Application')][string]$ApplicationDir,
    [Parameter(Mandatory, ParameterSetName='Installer')][string]$Installer,
    [Parameter(ParameterSetName='Installer')][string]$PreviousInstaller
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
$marker = $null
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

}
try {
    if ($Installer) {
        if ($PreviousInstaller) {
            Invoke-CheckedProcess -FilePath $PreviousInstaller -Arguments $arguments -Log $setupLog.FullName
            Invoke-CheckedProcess -FilePath (Join-Path $ApplicationDir 'UchebaAI.exe') -Arguments '--smoke-test' -Log $smokeLog.FullName
            $marker = Join-Path $ApplicationDir 'upgrade-test-marker.txt'
            Set-Content -LiteralPath $marker -Value 'preserve' -Encoding ascii
        }
        Invoke-CheckedProcess -FilePath $Installer -Arguments $arguments -Log $setupLog.FullName
        if ($PreviousInstaller) {
            $expected = (Get-Item -LiteralPath $Installer).VersionInfo.ProductVersion.Trim()
            $actual = Get-ItemPropertyValue -LiteralPath (Join-Path 'HKCU:' "$registryRoot\$appKey") -Name DisplayVersion
            if ($actual -ne $expected) {
                throw "После обновления зарегистрирована версия $actual вместо $expected."
            }
        }
        if ($PreviousInstaller -and (Get-Content -Raw -LiteralPath $marker).Trim() -ne 'preserve') {
            throw 'Обновление изменило дополнительный файл в каталоге приложения.'
        }
    }
    Invoke-CheckedProcess -FilePath (Join-Path $ApplicationDir 'UchebaAI.exe') -Arguments '--smoke-test' -Log $smokeLog.FullName
    if (Test-Path -LiteralPath (Join-Path $ApplicationDir 'download-ffmpeg.txt')) {
        foreach ($tool in @(
            'ffmpeg.exe', 'ffprobe.exe', '_internal/av.libs',
            '_internal/PySide6/Qt6VirtualKeyboard.dll',
            '_internal/PySide6/Qt6Pdf.dll'
        )) {
            if (Test-Path -LiteralPath (Join-Path $ApplicationDir $tool)) {
                throw "В публикуемом пакете обнаружен исключённый компонент: $tool"
            }
        }
    } else {
        foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
            & (Join-Path $ApplicationDir $tool) -version | Select-Object -First 1
        }
    }
} finally {
    if ($Installer) {
        $uninstaller = Join-Path $ApplicationDir 'unins000.exe'
        $uninstallLog = New-TemporaryFile
        $uninstallArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=""$($uninstallLog.FullName)""")
        if (Test-Path -LiteralPath $uninstaller) {
            Invoke-CheckedProcess -FilePath $uninstaller -Arguments $uninstallArguments -Log $uninstallLog.FullName
        }
        if ($marker -and (Test-Path -LiteralPath $marker)) {
            Remove-Item -LiteralPath $marker
        }
        if (Test-Path -LiteralPath (Join-Path $ApplicationDir 'UchebaAI.exe')) {
            throw 'Исполняемый файл остался после удаления.'
        }
    }
}
