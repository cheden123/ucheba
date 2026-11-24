param(
    [string]$Iscc,
    [string]$ExpectedTag,
    [switch]$TestInstaller
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

Push-Location (Join-Path $PSScriptRoot '..')
try {
    python -c "import struct, sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8"
    $version = python -c "from importlib.metadata import version; print(version('ucheba-ai'))"
    if ($version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw 'Версия пакета должна иметь вид MAJOR.MINOR.PATCH.'
    }
    if ($ExpectedTag -and $ExpectedTag -cne "v$version") {
        throw "Тег $ExpectedTag не совпадает с версией пакета v$version."
    }

    $build = New-Item -ItemType Directory -Force -Path 'build'
    $output = New-Item -ItemType Directory -Force -Path 'release'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $build.FullName 'pyinstaller-cache'
    $dist = Join-Path $build.FullName 'frozen'
    $arguments = @(
        '--noconfirm', '--clean', '--onedir', '--windowed', '--name', 'UchebaAI',
        '--paths', 'src', '--specpath', $build.FullName, '--workpath', (Join-Path $build.FullName 'pyinstaller'),
        '--distpath', $dist, '--collect-data', 'docx', '--collect-data', 'faster_whisper',
        '--collect-data', 'ucheba', '--copy-metadata', 'ucheba-ai', '--copy-metadata', 'keyring',
        '--hidden-import', 'keyring.backends.Windows', '--collect-binaries', 'ctranslate2',
        '--exclude-module', 'mypy', '--exclude-module', 'pytest', '--exclude-module', 'pip',
        '--exclude-module', 'pip_audit', '--exclude-module', 'ruff'
    )
    if (Test-Path -LiteralPath 'prompts' -PathType Container) {
        $arguments += @('--add-data', 'prompts:prompts')
    }
    python -m PyInstaller @arguments 'src/ucheba/__main__.py'

    $package = Join-Path $dist 'UchebaAI'
    & "$PSScriptRoot/get-ffmpeg.ps1" -Destination $package
    foreach ($name in @('README.md', 'CHANGELOG.md', 'LICENSE')) {
        if (Test-Path -LiteralPath $name -PathType Leaf) {
            Copy-Item -LiteralPath $name -Destination $package -Force
        }
    }
    python 'scripts/copy-notices.py' $package
    & "$PSScriptRoot/test-windows-package.ps1" -ApplicationDir $package

    $portable = Join-Path $output.FullName 'UchebaAI-Portable-x64'
    python -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" $portable $package

    if (!$Iscc) { $Iscc = & "$PSScriptRoot/get-innosetup.ps1" }
    & $Iscc "/DAppVersion=$version" "/DAppSource=$package" "/DAppOutput=$($output.FullName)" 'installer/UchebaAI.iss'

    $setup = Join-Path $output.FullName 'UchebaAI-Setup-x64.exe'
    if ($TestInstaller) {
        & "$PSScriptRoot/test-windows-package.ps1" -Installer $setup
    }
    $assets = @($setup, "$portable.zip")
    $assets | ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $(Split-Path -Leaf $_)"
    } | Set-Content -LiteralPath (Join-Path $output.FullName 'SHA256SUMS.txt') -Encoding ascii
} finally {
    Pop-Location
}
