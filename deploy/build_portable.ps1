<#
.SYNOPSIS
  Build a portable extract-and-run folder for Windows (middle or platform).

.DESCRIPTION
  Layout (relocatable, no system Python / no C:\d2a required):

    d2a-portable-<role>-<ver>\
      data2agent.exe        # single entry (pass -LauncherExe)
      runtime\              # CPython embeddable + site-packages
      app\templates\
      config\
      data\logs\
      README.txt

  Requires: Windows, network (download embeddable + get-pip), local wheels +
  a built data2agent wheel in -WheelsDir / dist.

.EXAMPLE
  .\deploy\build_portable.ps1 -Role middle -Version v0.1.7 -LauncherExe dist\launchers\middle\data2agent.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('middle', 'platform')]
    [string]$Role,

    [string]$Version = 'dev',

    [string]$OutDir = 'dist/portable',

    [string]$WheelsDir = '',

    [string]$WheelFile = '',

    [string]$PyFullVersion = '',

    [string]$LauncherExe = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not $PyFullVersion) {
    $PyFullVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    if ($LASTEXITCODE -ne 0) { throw 'python not available; cannot detect version' }
}
$pyParts = $PyFullVersion -split '\.'
$pyTag = '{0}{1}' -f $pyParts[0], $pyParts[1]   # 314
$embedName = "python-$PyFullVersion-embed-amd64.zip"
$embedUrl = "https://www.python.org/ftp/python/$PyFullVersion/$embedName"

if (-not $WheelsDir) { $WheelsDir = Join-Path $root 'stage/wheels' }
if (-not (Test-Path $WheelsDir)) { throw "WheelsDir not found: $WheelsDir" }

if (-not $WheelFile) {
    $cand = Get-ChildItem (Join-Path $root 'dist') -Filter 'data2agent-*.whl' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $cand) { throw 'No data2agent-*.whl in dist/; run python -m build first' }
    $WheelFile = $cand.FullName
}
Copy-Item -Force $WheelFile (Join-Path $WheelsDir (Split-Path -Leaf $WheelFile))

$pkgName = "d2a-portable-$Role-$Version"
$portable = Join-Path $OutDir $pkgName
if (Test-Path $portable) { Remove-Item -Recurse -Force $portable }
New-Item -ItemType Directory -Force -Path $portable | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $portable 'runtime') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $portable 'app\templates') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $portable 'config') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $portable 'data\logs') | Out-Null

# --- 1. Embeddable CPython -------------------------------------------------
Write-Step "Download embeddable CPython $PyFullVersion"
$embedZip = Join-Path $env:TEMP $embedName
if (-not (Test-Path $embedZip)) {
    Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
}
Expand-Archive -Path $embedZip -DestinationPath (Join-Path $portable 'runtime') -Force

$pth = Get-ChildItem (Join-Path $portable 'runtime') -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { throw 'python*._pth missing in embeddable package' }
$pthText = @(
    "python$pyTag.zip",
    '.',
    'Lib\site-packages',
    'import site'
) -join "`n"
Set-Content -Path $pth.FullName -Value $pthText -Encoding ascii

# --- 2. pip into embeddable ------------------------------------------------
Write-Step 'Bootstrap pip (get-pip.py)'
$getPip = Join-Path $env:TEMP 'get-pip.py'
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
}
$runtimePy = Join-Path $portable 'runtime\python.exe'
& $runtimePy $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'get-pip failed' }

# --- 3. Offline install data2agent + extras --------------------------------
$extra = if ($Role -eq 'middle') { 'connect,middle_admin' } else { 'ingest,connect,mcp,console' }
Write-Step "pip install data2agent[$extra] (offline from $WheelsDir)"
& $runtimePy -m pip install --no-index --find-links=$WheelsDir "data2agent[$extra]"
if ($LASTEXITCODE -ne 0) { throw 'pip install data2agent failed' }

# --- 4. Templates (not inside wheel) ---------------------------------------
Write-Step 'Copy templates -> app/templates'
Copy-Item -Recurse -Force (Join-Path $root 'templates\*') (Join-Path $portable 'app\templates')

# --- 4b. Vue Console dist (platform; required for /v1) ----------------------
if ($Role -eq 'platform') {
    $vueDist = Join-Path $root 'console-ui\dist'
    $vueIndex = Join-Path $vueDist 'index.html'
    if (-not (Test-Path $vueIndex)) {
        throw "console-ui/dist/index.html missing; run: cd console-ui; npm ci; npm run build"
    }
    Write-Step 'Copy Vue dist -> app/console-ui/dist'
    $destDist = Join-Path $portable 'app\console-ui\dist'
    New-Item -ItemType Directory -Force -Path $destDist | Out-Null
    Copy-Item -Recurse -Force (Join-Path $vueDist '*') $destDist
    if (-not (Test-Path (Join-Path $destDist 'index.html'))) {
        throw 'portable Vue dist copy failed: index.html missing'
    }
}

# --- 5. README (single entry: data2agent.exe, added by release / -LauncherExe) ---
Write-Step 'Write README.txt'
$v1Note = if ($Role -eq 'platform') {
    "  5. Vue Console: open http://127.0.0.1:8849/v1/ (requires app\console-ui\dist)."
} else { '' }
$readme = @"
data2agent portable ($Role) $Version
====================================

Extract anywhere. Double-click data2agent.exe — that is the only entry.

  1. First run opens the browser setup page.
  2. Tray icon (system tray): Open admin UI / Quit.
  3. If already running, double-click only reopens the admin UI.
  4. Quit from tray stops services started by this app.
$v1Note
Middle also needs Microsoft ODBC Driver 18 for SQL Server (MSI).

Keep this folder intact (runtime\ must stay next to data2agent.exe).
"@
Set-Content -Path (Join-Path $portable 'README.txt') -Value $readme -Encoding utf8

if ($LauncherExe -and (Test-Path $LauncherExe)) {
    Write-Step "Copy launcher -> data2agent.exe"
    Copy-Item -Force $LauncherExe (Join-Path $portable 'data2agent.exe')
}

# --- 6. Zip ----------------------------------------------------------------
Write-Step "Zip $pkgName.zip"
$zipPath = Join-Path $OutDir "$pkgName.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $portable -DestinationPath $zipPath -Force

Write-Host "Done: $zipPath"
Write-Host "Folder: $portable"
Get-ChildItem $portable | ForEach-Object { Write-Host ("  {0}" -f $_.Name) }
