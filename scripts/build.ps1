param([string]$InnoCompiler = "", [switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'Dependency synchronization failed' }
uv run pyinstaller --clean --noconfirm packaging/TraceAtlas.spec
if ($LASTEXITCODE -ne 0) { throw 'Application packaging failed' }
uv run python scripts/dependency_notices.py dist/TraceAtlas
if ($LASTEXITCODE -ne 0) { throw 'Dependency notice generation failed' }
if (-not $SkipInstaller) {
    if (-not $InnoCompiler) { $InnoCompiler = Join-Path $repoRoot '.tools/InnoSetup/ISCC.exe' }
    if (-not (Test-Path -LiteralPath $InnoCompiler)) { throw 'Install Inno Setup 6.7.3 or pass -InnoCompiler with its ISCC.exe path.' }
    & $InnoCompiler 'packaging/TraceAtlas.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
}
Get-ChildItem -LiteralPath dist -Filter '*.exe' | ForEach-Object {
    $fileHash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    ($fileHash.Hash.ToLowerInvariant() + '  ' + $_.Name) | Set-Content -LiteralPath ($_.FullName + '.sha256') -Encoding ascii
    $fileHash
} | Format-Table -AutoSize
