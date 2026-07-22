# 便携包部署

## 1. Release 附件

| 附件 | 部署位置 |
| --- | --- |
| `d2a-portable-platform-<版本>.zip` | 数据平台 |
| `d2a-portable-middle-<版本>.zip` | 中间服务器 |

两台机器必须使用同一 Release 版本。

## 2. 端口

| 机器 | 端口 | 用途 |
| --- | --- | --- |
| 中间服务器 | `8851` | 管理界面 |
| 数据平台 | `8849` | 管理界面 |
| 数据平台 | `8850` | ingest 接收 |
| 数据平台 | `8848` | MCP HTTP |

## 3. 部署步骤

1. 数据平台解压 `d2a-portable-platform-<版本>.zip`。
2. 数据平台双击 `data2agent.exe`。
3. 浏览器打开 `/v1/setup`,填写 ingest Token、管理 Token、MCP Token。
4. 中间服务器安装 ODBC Driver 18 for SQL Server。
5. 中间服务器解压 `d2a-portable-middle-<版本>.zip`。
6. 中间服务器双击 `data2agent.exe`。
7. 浏览器打开 `/config`,填写:
   - 平台 URL:`http://<平台IP>:8850` 或 `https://<平台域名>`
   - ERP 连接信息
   - 与平台一致的 ingest Token
   - 管理 Token
8. 确认两台机器托盘「运行状态」均为正常。

## 4. 验收

按 [push-validation.md](push-validation.md) 执行链路验收。

## 5. 文件位置

解压目录内:

```text
data2agent.exe
runtime\
app\templates\
config\
data\
data\logs\
```

凭据写入 `config\secrets.env`,配置写入 `config\*.yaml`。
