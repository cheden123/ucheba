$ErrorActionPreference = 'Stop'

$compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($compiler) { return $compiler.Source }

$installed = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Inno Setup 6\ISCC.exe'
if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }

$work = Join-Path $PSScriptRoot '..\build\innosetup'
New-Item -ItemType Directory -Force -Path $work | Out-Null
$work = (Resolve-Path -LiteralPath $work).Path
$download = Join-Path $work 'innosetup-6.7.4.exe'
$installed = Join-Path $work 'compiler\ISCC.exe'
if (!(Test-Path -LiteralPath $installed -PathType Leaf)) {
    if (!(Test-Path -LiteralPath $download -PathType Leaf)) {
        Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe' -OutFile $download
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $download
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.GetNameInfo('SimpleName', $false) -ne 'Pyrsys B.V.') {
        throw 'Подпись установщика Inno Setup не прошла проверку.'
    }
    $directory = Join-Path $work 'compiler'
    $process = Start-Process -FilePath $download -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/SP-', "/DIR=""$directory"""
    ) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0 -or !(Test-Path -LiteralPath $installed -PathType Leaf)) {
        throw "Не удалось установить Inno Setup (код $($process.ExitCode))."
    }
}
return $installed
