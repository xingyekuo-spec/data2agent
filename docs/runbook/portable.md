# 便携包部署(解压即用)

> U 盘拷贝一个 zip → 解压 → **双击 `data2agent.exe`**。  
> 无需系统 Python、无需 pip、无需固定 `C:\d2a`。每个包只有这一个入口。
>
> 推送链路验收清单见 [push-validation.md](push-validation.md)。

## 产物

| Release 附件 | 用途 |
| --- | --- |
| `d2a-portable-middle-<版本>.zip` | 中间服务器 |
| `d2a-portable-platform-<版本>.zip` | 数据平台 |

解压后:

```
d2a-portable-middle-<版本>\
  data2agent.exe     ← 只双击这个
  runtime\
  app\templates\
  config\            # connect.yaml + secrets.env(首次配置后生成)
  data\              # *.sqlite
  data\logs\
  README.txt
```

## 端口

| 角色 | 端口 | 用途 |
| --- | --- | --- |
| 中间机 | **8851** | 管理界面(`middle_admin`) |
| 平台机 | **8849** | 管理界面(`console`) |
| 平台机 | **8850** | ingest 接收(中间机出站目标;防火墙仅对中间机 IP) |
| 平台机 | **8848** | MCP HTTP(Agent 接入) |

## 用法

1. **中间机**:先装 [ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)(微软 MSI,一次即可)。
2. 解压对应 zip(两台机**同版本**)。**先平台后中间**。
3. 双击 `data2agent.exe`:
   - 首次:浏览器填配置
     - 平台:`/config` 填接收口令(ingest Token)与管理 Token
     - 中间:`/config` 填平台 URL(`http://<平台IP>:8850`)、ERP、同一接收口令
   - 之后:打开管理界面,并自动拉起后台服务  
     (中间机 connector;平台 ingest + apply + mcp)
   - 右下角托盘图标常驻:**打开管理界面** / **运行状态…** / **退出**(退出会停止本程序拉起的后台进程)
   - 后台进程崩溃会**自动重启**;若 60 秒内反复崩溃 5 次则停止重试并在托盘「运行状态」标红,详情见 `data\logs\d2a-launcher.log`
   - 若已在运行,再次双击只会重新打开管理界面,不会重复启动
   - 出错排查:管理界面「日志」页可直接查看各进程日志(页面 500 报错栈在 `管理界面`/`console` 日志,进程重启在 `启动器/launcher` 日志),无需登录服务器翻文件
4. 按 [push-validation.md §4A](push-validation.md) 做链路验收。

## 注意

- 整夹搬迁可以;不要只拷走 `data2agent.exe`。
- 凭据在 `config\secrets.env`(不进 YAML)。
- 备选安装(系统 Python + venv / NSSM):见 [install-middle.md](install-middle.md) · [install-platform.md](install-platform.md) · [windows-deploy.md](windows-deploy.md)。
