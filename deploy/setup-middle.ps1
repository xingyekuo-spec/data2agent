<#
.SYNOPSIS
  生成中间服务器(Pattern A:抽取 ERP → 推送数据平台)的 data2agent 配置,
  并把凭据写入机器级环境变量。全程参数化 + 安全提示,避免手工编辑 YAML / 连接串出错。

.DESCRIPTION
  执行内容:
    1. 校验以管理员身份运行(设置机器级环境变量所需);
    2. 安全提示输入 ERP 只读密码与平台 ingest token(不回显、不进命令历史);
    3. 生成 <ConfigDir>\connect.yaml(已存在则先备份为 .bak-时间戳);
    4. 设置机器级环境变量 D2A_E10_DSN(含连接串)与 D2A_INGEST_TOKEN;
    5. 用 venv 里的 Python 调 load_config 自检配置合法性。

  凭据纪律:连接串/token 只进环境变量,connect.yaml 里只留 dsn_env 名称。

.EXAMPLE
  # 以管理员身份打开 PowerShell,然后:
  .\setup-middle.ps1 -PlatformIP 10.0.0.5 -ErpServer erp-host -ErpDatabase E10 -ErpUser d2a_reader
  # 随后按提示输入 ERP 密码与 ingest token。

.NOTES
  完整部署步骤见 docs/runbook/windows-deploy.md。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, HelpMessage = "数据平台机内网 IP 或主机名")]
    [string]$PlatformIP,
    [int]$PlatformPort = 8850,

    [Parameter(Mandatory, HelpMessage = "ERP(E10)数据库主机名或 IP")]
    [string]$ErpServer,
    [int]$ErpPort = 1433,

    [Parameter(Mandatory, HelpMessage = "ERP 数据库名(E10 库)")]
    [string]$ErpDatabase,

    [Parameter(Mandatory, HelpMessage = "ERP 只读 SQL 账号(仅 SELECT 权限)")]
    [string]$ErpUser,

    [string]$AppDir       = 'C:\d2a\app',
    [string]$ConfigDir    = 'C:\d2a\config',
    [string]$DataDir      = 'C:\d2a\data',
    [string]$VenvPython   = 'C:\d2a\venv\Scripts\python.exe',
    [string]$OdbcDriver   = 'ODBC Driver 18 for SQL Server',
    [string]$SyncEvery    = '30m',
    [string]$Lookback     = '3d',

    # 跳过配置自检(venv 未装好时可用)
    [switch]$SkipValidate
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- 1. 管理员校验 ---------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    throw "请以【管理员身份】运行 PowerShell 后再执行本脚本(设置机器级环境变量需要)。"
}

# --- 2. 安全提示输入凭据 ---------------------------------------------------
Write-Step "输入凭据(不会回显)"
$erpPwdSec   = Read-Host "ERP 只读账号 [$ErpUser] 的密码" -AsSecureString
$tokenSec    = Read-Host "平台 ingest token(D2A_INGEST_TOKEN,须与平台机一致)" -AsSecureString

$erpPwd = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($erpPwdSec))
$token  = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokenSec))

if ([string]::IsNullOrWhiteSpace($erpPwd)) { throw "ERP 密码不能为空。" }
if ([string]::IsNullOrWhiteSpace($token))  { throw "ingest token 不能为空。" }
if ($erpPwd -match '[;{}]') {
    Write-Warning "ERP 密码含 ; { } 等字符,ODBC 连接串可能解析异常。建议改用不含这些字符的密码,或手工核对 D2A_E10_DSN。"
}

# --- 3. 生成 connect.yaml --------------------------------------------------
Write-Step "写入配置 $ConfigDir\connect.yaml"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir   | Out-Null

$cfgPath  = Join-Path $ConfigDir 'connect.yaml'
if (Test-Path $cfgPath) {
    $bak = "$cfgPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $cfgPath $bak -Force
    Write-Host "  已备份旧配置 → $bak"
}

$templatesDir = Join-Path $AppDir 'templates'
$landingPath  = Join-Path $DataDir 'middle.sqlite'
$sinkUrl      = "http://${PlatformIP}:${PlatformPort}"

# 推送模式:不写 reconcile_at(跨机对账 E6b 未实现,配了会被 load_config 拒绝)
$yaml = @"
# 由 setup-middle.ps1 生成于 $(Get-Date -Format s) —— 请勿手工填连接串,凭据在环境变量。
templates: $templatesDir
landing: $landingPath
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN
    whitelist_from_bindings: true
    windows: []
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: $Lookback
    sync_every: $SyncEvery
    sink: { type: http, url: "$sinkUrl", token_env: D2A_INGEST_TOKEN }
"@
Set-Content -Path $cfgPath -Value $yaml -Encoding utf8

# --- 4. 设置机器级环境变量 -------------------------------------------------
Write-Step "设置机器级环境变量(D2A_E10_DSN / D2A_INGEST_TOKEN)"
$dsn = "DRIVER={$OdbcDriver};SERVER=$ErpServer,$ErpPort;UID=$ErpUser;PWD=$erpPwd;DATABASE=$ErpDatabase;TrustServerCertificate=yes"
[Environment]::SetEnvironmentVariable("D2A_E10_DSN", $dsn, "Machine")
[Environment]::SetEnvironmentVariable("D2A_INGEST_TOKEN", $token, "Machine")
# 同时写入当前进程,供下方自检与本窗口后续命令使用
$env:D2A_E10_DSN = $dsn
$env:D2A_INGEST_TOKEN = $token
Write-Host "  已设置(密码/token 不回显)。SERVER=$ErpServer,$ErpPort  DATABASE=$ErpDatabase  UID=$ErpUser  → sink $sinkUrl"

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
    Write-Warning "未找到 $VenvPython,跳过自检。装好 venv 后可加 -SkipValidate 重跑或手工验证。"
}

Write-Host ""
Write-Step "完成。后续:"
Write-Host "  1. 机器级环境变量对新进程生效 —— 请【新开】PowerShell 窗口再跑服务;"
Write-Host "  2. 确保平台机 ingest 接收端已在 $sinkUrl 监听;"
Write-Host "  3. 冒烟验证(新窗口):"
Write-Host "     $VenvPython -m data2agent.connect serve --config $cfgPath --once" -ForegroundColor Yellow
