<#
.SYNOPSIS
  Build the single portable entry exe: data2agent.exe (per role).

.DESCRIPTION
  Thin PyInstaller one-file. Starts admin UI (+ workers when configured)
  using the portable folder's runtime\python.exe.

.EXAMPLE
  .\deploy\build_ui_launchers.ps1
  .\deploy\build_ui_launchers.ps1 -OutDir dist\launchers
#>
[CmdletBinding()]
param(
    [string]$OutDir = "dist/launchers"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

python -m pip install -q "pyinstaller>=6.0"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path "$OutDir/middle" | Out-Null
New-Item -ItemType Directory -Force -Path "$OutDir/platform" | Out-Null

$common = @(
    "--onefile",
    "--noconsole",
    "--clean",
    "--paths", "scripts",
    "--workpath", "build/pyinstaller",
    "--specpath", "build/pyinstaller"
)

Write-Host "==> Building middle data2agent.exe"
python -m PyInstaller @common --name data2agent --distpath "$OutDir/middle" scripts/entry_middle_ui.py

Write-Host "==> Building platform data2agent.exe"
python -m PyInstaller @common --name data2agent --distpath "$OutDir/platform" scripts/entry_platform_ui.py

Write-Host "Done:"
Get-ChildItem $OutDir -Recurse -Filter "data2agent.exe" | ForEach-Object {
    Write-Host ("  {0}  ({1} KB)" -f $_.FullName, [int]($_.Length / 1KB))
}
