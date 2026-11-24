param([Parameter(Mandatory)][string]$Destination)
$ErrorActionPreference = 'Stop'

$archiveName = 'ffmpeg-9.1-essentials_build.zip'
$downloadUrl = "https://github.com/GyanD/codexffmpeg/releases/download/9.1/$archiveName"
$expectedHash = 'b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd'
$cache = Join-Path $PSScriptRoot '..\build\ffmpeg-release'
New-Item -ItemType Directory -Force -Path $cache, $Destination | Out-Null
$archive = Join-Path $cache $archiveName
if (!(Test-Path -LiteralPath $archive) -or
    (Get-FileHash -LiteralPath $archive).Hash -ne $expectedHash) {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archive -TimeoutSec 180
}
if ((Get-FileHash -LiteralPath $archive).Hash -ne $expectedHash) {
    throw 'Контрольная сумма комплекта FFmpeg не совпадает.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $cache -Force
$expanded = Join-Path $cache 'ffmpeg-9.1-essentials_build'
foreach ($file in @('ffmpeg.exe', 'ffprobe.exe')) {
    Copy-Item -LiteralPath (Join-Path $expanded "bin\$file") -Destination $Destination -Force
}

$notices = Join-Path $Destination 'licenses\FFmpeg'
New-Item -ItemType Directory -Path $notices -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $expanded 'LICENSE') -Destination $notices -Force
@'
FFmpeg 9.1 Essentials — отдельные исполняемые файлы FFmpeg и FFprobe, GPLv3.
Поставщик бинарных файлов: Gyan Doshi, https://www.gyan.dev/ffmpeg/builds/
Точный архив: https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.1-essentials_build.zip
Исходный код FFmpeg: https://github.com/FFmpeg/FFmpeg/tree/74e4393595
Состав сборки: параметр configuration в выводе ffmpeg.exe -version.
'@ | Set-Content -LiteralPath (Join-Path $notices 'Источники.txt') -Encoding utf8
