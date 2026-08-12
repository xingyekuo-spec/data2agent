<# 以管理员 PowerShell 运行：把便携中间机安装为 SYSTEM 开机任务。 #>
[CmdletBinding()]
param([string]$TaskName = 'data2agent-middle')

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalNow = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalNow.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '请右键 PowerShell“以管理员身份运行”后重试。'
}

$home = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $home 'data2agent.exe'
if (-not (Test-Path $exe)) { throw "未找到 $exe" }
if (-not (Test-Path (Join-Path $home 'config\connect.yaml'))) {
    throw '请先双击 data2agent.exe 完成首次配置，再安装开机任务。'
}

$action = New-ScheduledTaskAction `
    -Execute $exe -Argument '--headless --no-browser' -WorkingDirectory $home
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$task = New-ScheduledTask `
    -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings `
    -Description 'data2agent 中间机：开机后无 GUI 常驻，崩溃自动重启'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$runDir = Join-Path $home 'data\run'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$status = @{
    installed = $true
    task_name = $TaskName
    checked_at = [DateTimeOffset]::Now.ToString('o')
} | ConvertTo-Json -Compress
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $runDir 'autostart-status.json'), $status, $utf8NoBom)
Write-Host "已安装并启动开机任务: $TaskName" -ForegroundColor Green
Write-Host "中间机目录: $home"
