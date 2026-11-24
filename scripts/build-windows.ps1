param(
    [string]$Iscc,
    [string]$ExpectedTag,
    [switch]$TestInstaller,
    [switch]$DownloadFFmpegOnFirstUse
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

Push-Location (Join-Path $PSScriptRoot '..')
try {
    $python = python -c "import json, sys; print(json.dumps(sys.executable))" | ConvertFrom-Json
    & $python -c "import struct, sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8"
    $version = & $python -c "from importlib.metadata import version; print(version('ucheba-ai'))"
    if ($version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw 'Версия пакета должна иметь вид MAJOR.MINOR.PATCH.'
    }
    if ($ExpectedTag -and $ExpectedTag -cne "v$version") {
        throw "Тег $ExpectedTag не совпадает с версией пакета v$version."
    }

    $build = New-Item -ItemType Directory -Force -Path 'build'
    $outputPath = if ($DownloadFFmpegOnFirstUse) { 'release/public' } else { 'release' }
    $output = New-Item -ItemType Directory -Force -Path $outputPath
    $buildSuffix = if ($DownloadFFmpegOnFirstUse) { '-public' } else { '' }
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $build.FullName "pyinstaller-cache$buildSuffix"
    $dist = Join-Path $build.FullName "frozen$buildSuffix"
    $specPath = New-Item -ItemType Directory -Force -Path (Join-Path $build.FullName "spec$buildSuffix")
    $versionFile = Join-Path $specPath.FullName 'app-version.txt'
    Set-Content -LiteralPath $versionFile -Value $version -Encoding ascii
    & $python "scripts/get-diarization-models.py"
    $arguments = @(
        '--noconfirm', '--clean', '--onedir', '--windowed', '--name', 'UchebaAI',
        '--paths', 'src', '--specpath', $specPath.FullName, '--workpath', (Join-Path $build.FullName "pyinstaller$buildSuffix"),
        '--distpath', $dist, '--collect-data', 'docx', '--collect-data', 'faster_whisper',
        '--collect-binaries', 'sherpa_onnx', '--collect-submodules', 'sherpa_onnx',
        '--add-data', "$($build.FullName)/diarization-models/segmentation.onnx:ucheba/models/diarization",
        '--add-data', "$($build.FullName)/diarization-models/segmentation-LICENSE.txt:ucheba/models/diarization",
        '--add-data', "$($build.FullName)/diarization-models/Источники.txt:ucheba/models/diarization",
        '--add-data', "${versionFile}:ucheba",
        '--collect-data', 'ucheba', '--copy-metadata', 'ucheba-ai', '--copy-metadata', 'keyring',
        '--hidden-import', 'keyring.backends.Windows', '--collect-binaries', 'ctranslate2',
        '--exclude-module', 'mypy', '--exclude-module', 'pytest', '--exclude-module', 'pip',
        '--exclude-module', 'pip_audit', '--exclude-module', 'ruff'
    )
    if ($DownloadFFmpegOnFirstUse) {
        $overlay = Join-Path $build.FullName 'frozen-overlay-public'
        & $python 'scripts/prepare-public-whisper.py' $overlay
        $arguments += @('--paths', $overlay, '--exclude-module', 'av')
    } else {
        $arguments += @('--add-data', "$($build.FullName)/diarization-models/embedding.onnx:ucheba/models/diarization")
    }
    if (Test-Path -LiteralPath 'prompts' -PathType Container) {
        $arguments += @('--add-data', "$PWD/prompts:prompts")
    }
    $previousPath = $env:PATH
    try {
        # PyInstaller checks PATH for DLLs. Qt needs Windows' ICU, while other
        # programs may put an incompatible ICU with the same name on PATH.
        $env:PATH = @(
            (Split-Path -Parent $python),
            (Join-Path $env:SystemRoot 'System32'),
            $env:SystemRoot
        ) -join [IO.Path]::PathSeparator
        & $python -m PyInstaller @arguments 'src/ucheba/__main__.py'
    } finally {
        $env:PATH = $previousPath
    }

    $package = Join-Path $dist 'UchebaAI'
    if ($DownloadFFmpegOnFirstUse) {
        $optionalQt = @(
            '_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll',
            '_internal/PySide6/Qt6VirtualKeyboard.dll',
            '_internal/PySide6/Qt6Qml.dll',
            '_internal/PySide6/Qt6QmlMeta.dll',
            '_internal/PySide6/Qt6QmlModels.dll',
            '_internal/PySide6/Qt6QmlWorkerScript.dll',
            '_internal/PySide6/Qt6Quick.dll',
            '_internal/PySide6/plugins/imageformats/qpdf.dll',
            '_internal/PySide6/Qt6Pdf.dll'
        )
        foreach ($asset in $optionalQt) {
            $path = Join-Path $package $asset
            if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path }
        }
        New-Item -ItemType File -Path (Join-Path $package 'download-ffmpeg.txt') | Out-Null
        New-Item -ItemType File -Path (Join-Path $package '_internal/ucheba/models/diarization/embedding-download.txt') | Out-Null
    } else {
        & "$PSScriptRoot/get-ffmpeg.ps1" -Destination $package
    }
    foreach ($name in @('README.md', 'CHANGELOG.md', 'LICENSE')) {
        if (Test-Path -LiteralPath $name -PathType Leaf) {
            Copy-Item -LiteralPath $name -Destination $package -Force
        }
    }
    & $python 'scripts/copy-notices.py' $package
    Copy-Item -LiteralPath 'docs/THIRD-PARTY-SOURCES.md' -Destination $output.FullName -Force
    & "$PSScriptRoot/test-windows-package.ps1" -ApplicationDir $package

    $portable = Join-Path $output.FullName 'UchebaAI-Portable-x64'
    & $python -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" $portable $package

    if (!$Iscc) { $Iscc = & "$PSScriptRoot/get-innosetup.ps1" }
    $installerArgs = @("/DAppVersion=$version", "/DAppSource=$package", "/DAppOutput=$($output.FullName)")
    if ($DownloadFFmpegOnFirstUse) { $installerArgs += '/DExternalMedia=1' }
    & $Iscc @installerArgs 'installer/UchebaAI.iss'

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
