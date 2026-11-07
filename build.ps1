# Builds the release executables into dist\ and the installer into installer\.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1            build, test and package
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipTests build and package only
#
# Needs Rust (cargo) and Inno Setup 6 (ISCC.exe: on the PATH or in its default folders).

param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
Write-Host "Total Battle Chest Collector $version"

if (-not $SkipTests) {
    cargo test --release
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}
cargo build --release
if ($LASTEXITCODE -ne 0) { throw "build failed" }

New-Item -ItemType Directory -Force dist | Out-Null
Copy-Item target\release\tbcollector.exe, target\release\tbcollector-config.exe, README.md, LICENSE, change.log dist\ -Force

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }

& $iscc "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
Write-Host "dist\ and installer\TBChestCollector-Setup-$version.exe are ready."
