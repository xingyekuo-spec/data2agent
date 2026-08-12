<# 卸载 SYSTEM 开机任务；不删除配置、状态库、备份或日志。 #>
[CmdletBinding()]
param([string]$TaskName = 'data2agent-middle')

$ErrorActionPreference = 'Stop'
$home = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir = Join-Path $home 'data\run'
$taskFound = $true
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "开机任务不存在: $TaskName"
    $taskFound = $false
} else {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$status = @{
    installed = $false
    task_name = $TaskName
    checked_at = [DateTimeOffset]::Now.ToString('o')
} | ConvertTo-Json -Compress
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $runDir 'autostart-status.json'), $status, $utf8NoBom)
if ($taskFound) {
    Write-Host "已卸载开机任务: $TaskName。数据与配置未删除。" -ForegroundColor Green
}
