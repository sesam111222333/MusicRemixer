param(
  [string]$Configuration = "release",
  [string]$OutputRoot = "dist",
  [string]$PackageName = "StemDeck-Windows-x64",
  [switch]$SkipTauriBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
  throw "This packaging script must run on Windows."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stage = Join-Path $Root "$OutputRoot\$PackageName"
$ZipPath = Join-Path $Root "$OutputRoot\$PackageName.zip"
$ChecksumPath = "$ZipPath.sha256"
$PythonDir = Join-Path $Stage "python"
$PythonExe = Join-Path $PythonDir "Scripts\python.exe"
$BackendDir = Join-Path $Stage "backend"
$DesktopDir = Join-Path $Root "desktop"
$TauriDir = Join-Path $DesktopDir "src-tauri"
$TargetExe = Join-Path $TauriDir "target\$Configuration\stemdeck.exe"

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command not found on PATH: $Name"
  }
}

function Copy-Tree([string]$Source, [string]$Destination) {
  if (Test-Path $Destination) {
    Remove-Item -Recurse -Force $Destination
  }
  Copy-Item -Recurse -Force $Source $Destination
}

Require-Command "node"
Require-Command "npm"
Require-Command "cargo"

if (-not (Get-Command "py" -ErrorAction SilentlyContinue) -and -not (Get-Command "python" -ErrorAction SilentlyContinue)) {
  throw "Python launcher not found. Install Python 3.12 on the Windows build agent."
}

if (Test-Path $Stage) {
  Remove-Item -Recurse -Force $Stage
}
if (Test-Path $ZipPath) {
  Remove-Item -Force $ZipPath
}
if (Test-Path $ChecksumPath) {
  Remove-Item -Force $ChecksumPath
}

New-Item -ItemType Directory -Force $Stage | Out-Null
New-Item -ItemType Directory -Force $BackendDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Stage "data") | Out-Null
foreach ($Dir in @("cache", "downloads", "ffmpeg", "jobs", "logs", "models")) {
  New-Item -ItemType Directory -Force (Join-Path $Stage "data\$Dir") | Out-Null
}

Copy-Tree (Join-Path $Root "app") (Join-Path $BackendDir "app")
Copy-Tree (Join-Path $Root "static") (Join-Path $BackendDir "static")
Copy-Item -Force (Join-Path $Root "pyproject.toml") (Join-Path $BackendDir "pyproject.toml")
Copy-Item -Force (Join-Path $Root "uv.lock") (Join-Path $BackendDir "uv.lock")
Copy-Item -Force (Join-Path $Root "packaging\windows\README-WINDOWS.txt") (Join-Path $Stage "README-WINDOWS.txt")
Copy-Item -Force (Join-Path $Root "packaging\windows\THIRD_PARTY_NOTICES.txt") (Join-Path $Stage "THIRD_PARTY_NOTICES.txt")

if (Get-Command "py" -ErrorAction SilentlyContinue) {
  & py -3.12 -m venv $PythonDir
} else {
  & python -m venv $PythonDir
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install "$Root"
& $PythonExe -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile"

Push-Location $DesktopDir
try {
  if (Test-Path "package-lock.json") {
    npm ci
  } else {
    npm install
  }

  if (-not $SkipTauriBuild) {
    npm run tauri build
  }
} finally {
  Pop-Location
}

if (-not (Test-Path $TargetExe)) {
  throw "Tauri executable not found at $TargetExe"
}

Copy-Item -Force $TargetExe (Join-Path $Stage "StemDeck.exe")

Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -Force
$Hash = Get-FileHash -Algorithm SHA256 $ZipPath
Set-Content -Path $ChecksumPath -Value "$($Hash.Hash)  $PackageName.zip"

Write-Host "Portable folder staged at: $Stage"
Write-Host "Portable zip created at: $ZipPath"
Write-Host "Checksum created at: $ChecksumPath"
