# Build script - bumps the patch version on every run, then builds the exe.
# Usage:  .\build.ps1            (patch bump: 1.0.0 -> 1.0.1)
#         .\build.ps1 minor      (1.0.3 -> 1.1.0)
#         .\build.ps1 major      (1.4.2 -> 2.0.0)
param([ValidateSet('patch','minor','major')][string]$part = 'patch')

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# --- read current version -------------------------------------------------
$verFile = Join-Path $PSScriptRoot 'version.py'
$content = Get-Content $verFile -Raw
if ($content -notmatch 'VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"') {
    throw "Cannot read VERSION from version.py"
}
$maj = [int]$Matches[1]; $min = [int]$Matches[2]; $pat = [int]$Matches[3]

switch ($part) {
    'major' { $maj++; $min = 0; $pat = 0 }
    'minor' { $min++; $pat = 0 }
    'patch' { $pat++ }
}
$new = "$maj.$min.$pat"

# --- write it back --------------------------------------------------------
$content = $content -replace 'VERSION\s*=\s*"\d+\.\d+\.\d+"', "VERSION = `"$new`""
Set-Content -Path $verFile -Value $content -Encoding utf8 -NoNewline
Write-Host "Version -> $new" -ForegroundColor Cyan

# --- build ----------------------------------------------------------------
# PyInstaller logs to stderr; don't let that trip ErrorActionPreference=Stop.
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$ErrorActionPreference = 'Continue'
& $py -m PyInstaller --noconfirm --clean --name JitsiRecorder --onefile --console `
    --add-data "capture.js;." `
    --collect-all playwright --collect-all uvicorn --collect-submodules fastapi `
    server.py
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($code -ne 0) { throw "Build failed (exit $code)" }

# clean pyinstaller leftovers
Remove-Item -Recurse -Force (Join-Path $PSScriptRoot 'build'), `
    (Join-Path $PSScriptRoot 'JitsiRecorder.spec') -ErrorAction SilentlyContinue

# keep a versioned copy alongside the stable name
Copy-Item (Join-Path $PSScriptRoot 'dist\JitsiRecorder.exe') `
    (Join-Path $PSScriptRoot "dist\JitsiRecorder-$new.exe") -Force

Write-Host "Done: dist\JitsiRecorder.exe  and  dist\JitsiRecorder-$new.exe" -ForegroundColor Green
