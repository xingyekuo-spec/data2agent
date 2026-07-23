# 推送链路部署与验收

适用链路:

```text
ERP → 中间服务器 → 数据平台
```

## 1. 部署前确认

| # | 确认项 |
| --- | --- |
| 1 | 数据平台已拿到 `d2a-portable-platform-<版本>.zip` |
| 2 | 中间服务器已拿到 `d2a-portable-middle-<版本>.zip` |
| 3 | 两台机器使用同一 Release 版本 |
| 4 | 两台机器约定同一 ingest Token |
| 5 | 中间服务器已安装 ODBC Driver 18 for SQL Server |
| 6 | 中间服务器可以访问 ERP SQL Server |
| 7 | 中间服务器可以访问数据平台 ingest 端口,默认 `8850` |
| 8 | ERP 已创建只读账号 |
| 9 | 已确认需要抽取的表清单及各自的水位字段(增量表)或全量刷新(维表) |

## 2. 部署

### 2.1 数据平台

1. 解压 `d2a-portable-platform-<版本>.zip`。
2. 双击 `data2agent.exe`。
3. 浏览器打开 `/v1/setup`。
4. 填写:
   - ingest Token
   - 管理 Token
   - MCP Token
5. 保存配置。
6. 确认托盘「运行状态」正常。

### 2.2 中间服务器

1. 安装 ODBC Driver 18 for SQL Server。
2. 解压 `d2a-portable-middle-<版本>.zip`。
3. 双击 `data2agent.exe`。
4. 浏览器打开 `/config`。
5. 填写:
   - 平台 URL:`http://<平台IP>:8850` 或 `https://<平台域名>`
   - ERP 连接信息
   - 与数据平台一致的 ingest Token
   - 管理 Token
6. 保存配置。
7. 确认托盘「运行状态」正常。

## 3. 管理界面验收

| # | 位置 | 检查 | 期望 |
| --- | --- | --- | --- |
| 1 | 中间服务器 `:8851` | 状态页 | 有水位,最近运行成功 |
| 2 | 中间服务器 `:8851` | 日志页 | connector 无持续 ERROR |
| 3 | 数据平台 `:8849` | 仪表盘 | 出现 `raw_*` 数据 |
| 4 | 数据平台 `:8849` | 仪表盘 | 出现 `obj_*` 数据 |
| 5 | 数据平台 `:8849` | 日志页 | ingest / apply / mcp 无持续 ERROR |
| 6 | 两台机器 | 托盘「运行状态」 | 后台进程正常 |
| 7 | 中间服务器 `:8851` | 配置页 | `tables` 字段已声明抽取表,每表 `mode`(incremental/full_refresh)与 `watermark` 正确 |

## 4. 二次同步验收

1. 等待下一轮同步,或在中间服务器管理界面触发一次同步。
2. 打开数据平台管理界面。
3. 确认 `raw_*` 行数没有因重复推送异常膨胀。
4. 确认对象层 `obj_*` 仍可正常浏览。
