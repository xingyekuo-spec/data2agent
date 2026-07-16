<#
.SYNOPSIS
  Build Windows double-click launchers: d2a-middle-ui.exe / d2a-platform-ui.exe

.DESCRIPTION
  Thin PyInstaller one-file exes. They start the already-installed venv modules
  (data2agent.middle_admin / data2agent.console) and open the browser.
  Run on Windows with Python 3.14 available.

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

$common = @(
    "--onefile",
    "--noconsole",
    "--clean",
    "--paths", "scripts",
    "--distpath", $OutDir,
    "--workpath", "build/pyinstaller",
    "--specpath", "build/pyinstaller"
)

Write-Host "==> Building d2a-middle-ui.exe"
python -m PyInstaller @common --name d2a-middle-ui scripts/entry_middle_ui.py

Write-Host "==> Building d2a-platform-ui.exe"
python -m PyInstaller @common --name d2a-platform-ui scripts/entry_platform_ui.py

Write-Host "Done:"
Get-ChildItem $OutDir -Filter "*.exe" | ForEach-Object { Write-Host "  $($_.FullName)  ($([int]($_.Length/1KB)) KB)" }
