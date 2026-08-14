"""换包脚本(apply-update.ps1)与启动入口(升级.bat)的内容模板与写入。

PS1 在「程序已退出」后运行,负责:校验暂存包 → 目录改名式换包 →
启动新版本 → 健康检查 → 失败自动回滚。全程不依赖 Python。

注意:PS1 必须以 UTF-8 with BOM 写入,否则 Windows PowerShell 5.1
会按 ANSI(GBK)解析,中文与脚本本身都可能损坏。
"""

from __future__ import annotations

from pathlib import Path

#: 换包时从暂存包移入 home 的条目(config/ 与 data/ 一律不动;
#: 「升级.bat」是脚本自身入口,cmd 执行期间被占用,不参与换包——
#: 它只是转发到 data/updates/apply-update.ps1 的引导,升级后由 console
#: 在下一次暂存时按最新模板重写)。
MOVE_ITEMS = (
    "data2agent.exe",
    "runtime",
    "app",
    "BUILD-INFO.json",
    "README.txt",
)

APPLY_PS1 = r"""# data2agent 平台便携包换包脚本 —— 由 console 在更新就绪时生成,请勿手改。
# 用法:退出托盘后双击便携包根目录的「升级.bat」;或手动:
#   powershell -ExecutionPolicy Bypass -File apply-update.ps1 -InstallDir C:\d2a -Staging C:\d2a\data\updates\staging
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$Staging,
    [int]$WaitPid = 0,          # 预留:一键「退出并升级」时等待该 PID 消失
    [int]$HealthTimeoutSec = 90
)
$ErrorActionPreference = 'Stop'
# 输出统一 UTF-8:现场窗口与 CI 捕获都避免中文乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 「升级.bat」不参与换包:它是本脚本的执行入口,cmd 执行期间文件被占用;
# bat 只是固定引导,新版本如有变化会在下次「检查更新」时由 console 重写。
$MoveItems = @('data2agent.exe', 'runtime', 'app', 'BUILD-INFO.json', 'README.txt')

$logDir = Join-Path $InstallDir 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'd2a-update.log'
function Log([string]$m) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Host $m
}
function Fail([string]$m) {
    Log "失败: $m"
    Write-Host ''
    Write-Host "升级失败: $m" -ForegroundColor Red
    Write-Host '详情见 data\logs\d2a-update.log'
    exit 1
}
function Test-ConsolePort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', 8849, $null, $null)
        return $async.AsyncWaitHandle.WaitOne(800) -and $client.Connected
    } catch { return $false } finally { $client.Close() }
}
function Stop-PortablePython {
    try {
        $cim = Get-Command Get-CimInstance -ErrorAction SilentlyContinue
        if (-not $cim) {
            Log '跳过 Python 进程清理:Get-CimInstance 不可用'
            return
        }
        Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -like '*data2agent.platform.console*' -and
                $_.CommandLine -like "*$InstallDir*"
            } |
            ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
    } catch {
        Log "跳过 Python 进程清理: $_"
    }
}

# 已动过的条目(改名或替换),供回滚恢复。条目在「改名 .old 之后、
# Move 之前」就登记,这样即使 Move 失败,已改名的一半也能被恢复。
$script:moved = @()
function Restore-PreviousVersion([string]$reason) {
    Log "回滚: $reason"
    Get-Process -Name 'data2agent' -ErrorAction SilentlyContinue | Stop-Process -Force
    Stop-PortablePython
    Start-Sleep -Seconds 2
    foreach ($item in $script:moved) {
        $dst = Join-Path $InstallDir $item
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        if (Test-Path "$dst.old") { Rename-Item -Path "$dst.old" -NewName $item }
        Log "已回滚 $item"
    }
}

try { $InstallDir = (Resolve-Path $InstallDir).Path } catch { Fail "Home 不存在: $InstallDir" }
Log "==== 开始升级 Home=$InstallDir Staging=$Staging WaitPid=$WaitPid ===="

# 1. 确认程序已退出(-WaitPid 模式先等待)
if ($WaitPid -gt 0) {
    Log "等待 PID $WaitPid 退出…"
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) { Fail '等待程序退出超时,请手动退出后重试' }
    Start-Sleep -Seconds 2
}
if (Get-Process -Name 'data2agent' -ErrorAction SilentlyContinue) {
    Fail '检测到 data2agent.exe 仍在运行,请先从托盘退出程序(右键托盘图标 → 退出)'
}
if (Test-ConsolePort) { Fail '管理界面(8849)仍在运行,请确认平台程序已完全退出' }

# 2. 定位并校验暂存更新包
$pkg = $null
if (Test-Path (Join-Path $Staging 'BUILD-INFO.json')) {
    $pkg = $Staging
} else {
    $dirs = @(Get-ChildItem $Staging -Directory -ErrorAction SilentlyContinue)
    if ($dirs.Count -eq 1) { $pkg = $dirs[0].FullName }
}
if (-not $pkg) { Fail '未找到待安装的更新包,请先在管理界面「检查更新」并完成下载' }
foreach ($item in @('data2agent.exe', 'runtime', 'app', 'BUILD-INFO.json')) {
    if (-not (Test-Path (Join-Path $pkg $item))) { Fail "更新包不完整,缺少 $item;请重新下载更新" }
}

# 3. 目录改名式换包(config\ 与 data\ 不动;旧版本改名 .old 保留以便回滚;
#    任何一步失败都通过 Restore-PreviousVersion 恢复,不留新旧混杂)
try {
    foreach ($item in $MoveItems) {
        $src = Join-Path $pkg $item
        if (-not (Test-Path $src)) { continue }
        $dst = Join-Path $InstallDir $item
        $old = "$dst.old"
        if (Test-Path $old) { Remove-Item -Recurse -Force $old }
        if (Test-Path $dst) { Rename-Item -Path $dst -NewName "$item.old" }
        # 先登记再 Move:Move 失败时回滚能把刚改名的 .old 恢复回来
        $script:moved += $item
        Move-Item -Path $src -Destination $dst
        Log "已更新 $item"
    }
} catch {
    Restore-PreviousVersion "换包失败: $_"
    Fail "换包失败:$_(已自动恢复旧版本,请重试或联系运维)"
}

# 4. 启动新版本
$exe = Join-Path $InstallDir 'data2agent.exe'
try {
    Start-Process -FilePath $exe -WorkingDirectory $InstallDir
} catch {
    Restore-PreviousVersion "新版本启动失败: $_"
    Fail '新版本无法启动,已自动回滚到旧版本'
}
Log '已启动新版本,等待健康检查…'

# 5. 健康检查;超时则自动回滚
$ok = $false
$deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-ConsolePort) { $ok = $true; break }
    Start-Sleep -Seconds 2
}

if (-not $ok) {
    Restore-PreviousVersion '健康检查超时'
    if (Test-Path $exe) { Start-Process -FilePath $exe -WorkingDirectory $InstallDir }
    Fail '新版本未能正常启动,已自动回滚到旧版本'
}

Log '升级完成,新版本已正常运行'
Write-Host ''
Write-Host '升级完成!管理界面: http://127.0.0.1:8849/' -ForegroundColor Green
Write-Host '(旧版本已保留为 .old,确认运行正常后可手动删除)'
"""

UPDATE_BAT = """@echo off
rem NOTE: keep this file pure ASCII. cmd.exe parses .bat in the console
rem codepage; UTF-8 Chinese text + chcp 65001 has known parse bugs that
rem can kill the window before pause runs (seen in field: flash-exit).
chcp 65001 >nul
setlocal
set "SCRIPT=%~dp0data\\updates\\apply-update.ps1"
if not exist "%SCRIPT%" (
  echo.
  echo   No staged update found.
  echo   Open the console (http://127.0.0.1:8849/ , Settings page),
  echo   run "Check update" and finish the download first.
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -InstallDir "%~dp0." -Staging "%~dp0data\\updates\\staging" %*
if errorlevel 1 pause
"""


def write_update_scripts(home: Path, updates_dir: Path) -> Path:
    """把换包脚本写入 data/updates,启动入口写到 home 根目录;返回 bat 路径。"""
    updates_dir.mkdir(parents=True, exist_ok=True)
    (updates_dir / "apply-update.ps1").write_text(APPLY_PS1, encoding="utf-8-sig")
    bat = home / "升级.bat"
    bat.write_text(UPDATE_BAT, encoding="utf-8")
    return bat
