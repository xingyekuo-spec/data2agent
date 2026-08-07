<# 卸载 SYSTEM 开机任务；不删除配置、状态库、备份或日志。 #>
[CmdletBinding()]
param([string]$TaskName = 'data2agent-middle')

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "开机任务不存在: $TaskName"
    exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已卸载开机任务: $TaskName。数据与配置未删除。" -ForegroundColor Green
