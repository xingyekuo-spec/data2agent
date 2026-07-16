# 中间服务器安装步骤

> 仅本机操作。平台机须先装好并启动 ingest(8850)。两台机使用**同一 Release 版本**。
> 详解见 [windows-deploy.md](windows-deploy.md)。

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

6. **管理员** PowerShell 生成配置并写入凭据(按提示输入 ERP 密码、与平台一致的 ingest token)。
   若报「在此系统上禁止运行脚本」,先执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
   命名实例(如 `HOST\SQLEXPRESS`)请给 `-ErpServer` 加引号;脚本会自动省略端口:
   ```powershell
   C:\d2a\app\setup-middle.ps1 -PlatformIP <平台机内网IP> -ErpServer 'DESKTOP-X\SQLEXPRESS' -ErpDatabase <E10库> -ErpUser d2a_reader
   ```
   默认实例(带端口):
   ```powershell
   C:\d2a\app\setup-middle.ps1 -PlatformIP <平台机内网IP> -ErpServer <ERP主机> -ErpDatabase <E10库> -ErpUser d2a_reader
   ```

7. **新开**普通 PowerShell 窗口(机器级环境变量对新进程生效)。

8. 确认平台机 ingest 已监听 8850 后,冒烟一次:
   ```powershell
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

   # d2a-middle-admin (port 8851; Token 从机器级 D2A_MIDDLE_ADMIN_TOKEN 继承,勿在 AppParameters 写 %VAR%)
   C:\d2a\nssm\nssm.exe install d2a-middle-admin C:\d2a\venv\Scripts\python.exe
   C:\d2a\nssm\nssm.exe set d2a-middle-admin AppParameters "-m data2agent.middle_admin --config C:\d2a\config\connect.yaml --host 0.0.0.0 --port 8851 --log-path C:\d2a\data\logs\d2a-connector.log"
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

11. 管理界面验收(浏览器,setup-middle 输出的 `D2A_MIDDLE_ADMIN_TOKEN` 登录):
    - 打开 `http://<本机内网IP>:8851`
    - 状态页应显示 sync 配置;配置页可查看 connect.yaml 白名单字段
