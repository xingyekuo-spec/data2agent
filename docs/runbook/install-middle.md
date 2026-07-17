# 中间服务器安装步骤

> **推荐新现场用便携包:**见 [portable.md](portable.md)(解压即用,无需系统 Python)。  
> 下文为旧版「系统 Python + venv + 离线 wheels」流程,仅作备选。
> 平台机须先装好并启动 ingest(8850)。两台机使用**同一 Release 版本**。

1. 安装 **Python 3.14 官方 64 位**(勾选 Add to PATH)。

2. 安装 [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)，确认:
   ```powershell
   Get-OdbcDriver -Name "ODBC Driver 18 for SQL Server"
   ```

3. 拿到 Release 附件 `d2a-runtime-connect-<版本>.zip`(与平台机同版本),拷到本机。

4. 建目录与虚拟环境:
   ```powershell
   New-Item -ItemType Directory -Force C:\d2a\app, C:\d2a\data, C:\d2a\config | Out-Null
   python -m venv C:\d2a\venv
   C:\d2a\venv\Scripts\python.exe -m pip install --upgrade pip
   ```

5. 解压并离线安装:
   ```powershell
   Expand-Archive d2a-runtime-connect-<版本>.zip C:\d2a\app
   C:\d2a\venv\Scripts\pip.exe install --no-index --find-links=C:\d2a\app\wheels -e C:\d2a\app[connect,middle_admin]
   ```

6. **首次配置(推荐浏览器,无需 PowerShell 脚本):**
   ```powershell
   C:\d2a\venv\Scripts\python.exe -m data2agent.middle_admin --home C:\d2a --host 127.0.0.1 --port 8851
   ```
   浏览器打开 `http://127.0.0.1:8851/config`,填写平台 URL、ERP、ingest/管理 Token。
   配置写入 `C:\d2a\config\connect.yaml` + `C:\d2a\config\secrets.env`(密码不进 YAML)。
   便携包现场请用 [portable.md](portable.md) 的 `data2agent.exe`。

   备选(管理员 PowerShell 脚本,仍可用):
   ```powershell
   C:\d2a\app\setup-middle.ps1 -PlatformIP <平台机内网IP> -ErpServer 'DESKTOP-X\SQLEXPRESS' -ErpDatabase <E10库> -ErpUser d2a_reader
   ```
   命名实例请给 `-ErpServer` 加引号;脚本会自动省略端口。默认实例可省略引号并带端口。

7. **新开**普通 PowerShell 窗口(若用了脚本写的机器级环境变量)。浏览器配置写入 `secrets.env`,管理界面与 connector 启动时会自动加载;若用 NSSM,请把 `secrets.env` 里的键同步到机器级环境变量,或让服务启动前加载该文件。

8. 确认平台机 ingest 已监听 8850 后,冒烟一次:
   ```powershell
   # 若用 secrets.env,先加载到当前会话,或已设机器级 D2A_E10_DSN / D2A_INGEST_TOKEN
   C:\d2a\venv\Scripts\python.exe -m data2agent.connect serve --config C:\d2a\config\connect.yaml --once
   ```

9. (可选)用 NSSM 装常驻服务(connector + 管理界面):
   ```powershell
   # d2a-connector (sync/push)
   C:\d2a\nssm\nssm.exe install d2a-connector C:\d2a\venv\Scripts\python.exe
   C:\d2a\nssm\nssm.exe set d2a-connector AppParameters "-m data2agent.connect serve --config C:\d2a\config\connect.yaml"
   C:\d2a\nssm\nssm.exe set d2a-connector AppDirectory C:\d2a\app
   C:\d2a\nssm\nssm.exe set d2a-connector AppStdout C:\d2a\data\logs\d2a-connector.log
   C:\d2a\nssm\nssm.exe set d2a-connector AppStderr C:\d2a\data\logs\d2a-connector.log
   C:\d2a\nssm\nssm.exe set d2a-connector AppExit Default Restart

   # d2a-middle-admin (port 8851; Token 从环境变量 / secrets.env 继承,勿在 AppParameters 写 %VAR%)
   C:\d2a\nssm\nssm.exe install d2a-middle-admin C:\d2a\venv\Scripts\python.exe
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppParameters "-m data2agent.middle_admin --home C:\d2a --host 0.0.0.0 --port 8851"
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppDirectory C:\d2a\app
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppStdout C:\d2a\data\logs\d2a-middle-admin.log
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppStderr C:\d2a\data\logs\d2a-middle-admin.log
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppExit Default Restart

   C:\d2a\nssm\nssm.exe start d2a-connector
   C:\d2a\nssm\nssm.exe start d2a-middle-admin
   ```
   防火墙:内网放行入站 **8851**(管理界面,仅对运维网段)。

10. 验收(本机应有水位、无 raw 表):
    ```powershell
    C:\d2a\venv\Scripts\python.exe -m data2agent.connect status --landing C:\d2a\data\middle.sqlite
    ```

11. 管理界面验收:浏览器打开 `http://127.0.0.1:8851`(或便携包双击 `data2agent.exe`)。
