param([Parameter(Mandatory)][string]$Tag)
$ErrorActionPreference = 'Stop'
if ($Tag -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Ожидался тег vMAJOR.MINOR.PATCH.' }

$version = [regex]::Escape($Tag.Substring(1))
$changelog = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../CHANGELOG.md') -Raw -Encoding utf8
$pattern = "(?ms)^##[ \t]+\[?v?$version\]?(?:[ \t][^\r\n]*)?\r?\n(.+?)(?=^##[ \t]|\z)"
$section = [regex]::Match($changelog, $pattern)
if (!$section.Success -or !$section.Groups[1].Value.Trim()) {
    throw "В CHANGELOG.md отсутствует раздел с изменениями $Tag."
}
$notes = Join-Path $PSScriptRoot '../release/notes.md'
New-Item -ItemType Directory -Path (Split-Path -Parent $notes) -Force | Out-Null
$section.Groups[1].Value.Trim() | Set-Content -LiteralPath $notes -Encoding utf8
