<#
.SYNOPSIS
  生成数据平台机(Pattern A 接收端:ingest + apply + MCP + 控制台)的 data2agent
  配置,并设置机器级 token 环境变量。参数化 + 安全提示,避免手工编辑出错。

.DESCRIPTION
  执行内容:
    1. 校验以管理员身份运行;
    2. 安全提示输入 D2A_INGEST_TOKEN(须与中间机一致);
    3. D2A_MCP_TOKEN / D2A_CONSOLE_TOKEN 未提供则自动生成随机长串;
    4. 生成 <ConfigDir>\platform.yaml(已存在则备份);
    5. 设置机器级环境变量;
    6. 用 venv 的 Python 调 load_config 自检;
    7. 打印四个服务(ingest/apply/mcp/console)的启动参数备查。

.EXAMPLE
  # 以管理员身份打开 PowerShell:
  .\setup-platform.ps1
  # 按提示输入 ingest token(与中间机同一串);mcp/console token 自动生成并显示。

.NOTES
  完整部署步骤见 docs/runbook/windows-deploy.md(§4.3 / §5.2)。
#>
[CmdletBinding()]
param(
    [string]$AppDir       = 'C:\d2a\app',
    [string]$ConfigDir    = 'C:\d2a\config',
    [string]$DataDir      = 'C:\d2a\data',
    [string]$VenvPython   = 'C:\d2a\venv\Scripts\python.exe',
    [int]$IngestPort      = 8850,
    [int]$McpPort         = 8848,
    [int]$ConsolePort     = 8849,
    [int]$ApplyEvery      = 1800,   # apply 常驻循环间隔(秒)

    # 留空则自动生成随机长串
    [string]$McpToken,
    [string]$ConsoleToken,

    [switch]$SkipValidate
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function New-Token {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 44 | ForEach-Object { [char]$_ })
}

# --- 1. 管理员校验 ---------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    throw "请以【管理员身份】运行 PowerShell 后再执行本脚本(设置机器级环境变量需要)。"
}

# --- 2. token:ingest 提示输入,mcp/console 缺省自动生成 -------------------
Write-Step "输入 / 生成 token"
$ingestSec = Read-Host "平台 ingest token(D2A_INGEST_TOKEN,须与中间机同一串)" -AsSecureString
$ingestToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ingestSec))
if ([string]::IsNullOrWhiteSpace($ingestToken)) { throw "ingest token 不能为空。" }

$mcpGenerated = $false
$consoleGenerated = $false
if ([string]::IsNullOrWhiteSpace($McpToken))     { $McpToken = New-Token;     $mcpGenerated = $true }
if ([string]::IsNullOrWhiteSpace($ConsoleToken)) { $ConsoleToken = New-Token; $consoleGenerated = $true }

# --- 3. 生成 platform.yaml -------------------------------------------------
Write-Step "写入配置 $ConfigDir\platform.yaml"
New-Item -ItemType Directory -Force -Path $ConfigDir            | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir             | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'logs') | Out-Null

$cfgPath = Join-Path $ConfigDir 'platform.yaml'
if (Test-Path $cfgPath) {
    $bak = "$cfgPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $cfgPath $bak -Force
    Write-Host "  已备份旧配置 → $bak"
}

$templatesDir = Join-Path $AppDir 'templates'
$landingPath  = Join-Path $DataDir 'factory.sqlite'

# 平台机只做 apply/接收,mssql adapter 仅用于读 binding、不真的连 ERP,dsn_env 用占位名。
$yaml = @"
# 由 setup-platform.ps1 生成于 $(Get-Date -Format s)
templates: $templatesDir
landing: $landingPath
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN_PLACEHOLDER
"@
Set-Content -Path $cfgPath -Value $yaml -Encoding utf8

# --- 4. 设置机器级环境变量 -------------------------------------------------
Write-Step "设置机器级环境变量(D2A_INGEST_TOKEN / D2A_MCP_TOKEN / D2A_CONSOLE_TOKEN)"
[Environment]::SetEnvironmentVariable("D2A_INGEST_TOKEN",  $ingestToken,  "Machine")
[Environment]::SetEnvironmentVariable("D2A_MCP_TOKEN",     $McpToken,     "Machine")
[Environment]::SetEnvironmentVariable("D2A_CONSOLE_TOKEN", $ConsoleToken, "Machine")
$env:D2A_INGEST_TOKEN  = $ingestToken
$env:D2A_MCP_TOKEN     = $McpToken
$env:D2A_CONSOLE_TOKEN = $ConsoleToken

# --- 5. 配置自检 -----------------------------------------------------------
if ($SkipValidate) {
    Write-Step "已跳过配置自检(-SkipValidate)"
}
elseif (Test-Path $VenvPython) {
    Write-Step "校验配置合法性(load_config)"
    & $VenvPython -c "from data2agent.connect.config import load_config; load_config(r'$cfgPath'); print('CONFIG OK')"
    if ($LASTEXITCODE -ne 0) { throw "配置自检失败,请检查上面报错。" }
}
else {
    Write-Warning "未找到 $VenvPython,跳过自检。"
}

# --- 6. 输出 token 与服务启动参数 ------------------------------------------
Write-Host ""
Write-Step "生成的 token(请妥善保存,客户端连接时需要):"
if ($mcpGenerated)     { Write-Host "  D2A_MCP_TOKEN     = $McpToken" -ForegroundColor Yellow }
                  else { Write-Host "  D2A_MCP_TOKEN     = (沿用传入值)" }
if ($consoleGenerated) { Write-Host "  D2A_CONSOLE_TOKEN = $ConsoleToken" -ForegroundColor Yellow }
                  else { Write-Host "  D2A_CONSOLE_TOKEN = (沿用传入值)" }
Write-Host "  D2A_INGEST_TOKEN  = (不回显,须与中间机一致)"

Write-Host ""
Write-Step "四个服务的 AppParameters(NSSM 装服务见 runbook §5.2):"
Write-Host "  d2a-ingest : -m data2agent.ingest --landing $landingPath --host 0.0.0.0 --port $IngestPort"
Write-Host "  d2a-apply  : -m data2agent.connect apply --config $cfgPath --landing $landingPath --every $ApplyEvery"
Write-Host "  d2a-mcp    : -m data2agent.mcp_server --db $landingPath --transport http --host 0.0.0.0 --port $McpPort"
Write-Host "  d2a-console: -m data2agent.console --config $cfgPath --host 0.0.0.0 --port $ConsolePort"

Write-Host ""
Write-Step "完成。下一步:"
Write-Host "  1. 机器级环境变量对新进程生效 —— 装/起服务前请【新开】PowerShell 窗口;"
Write-Host "  2. 先起 d2a-ingest(监听 $IngestPort),中间机才能推数据过来;"
Write-Host "  3. 冒烟(新窗口):先手动起 ingest,再到中间机跑 serve --once,回看本机 raw_ 表。" -ForegroundColor Yellow
