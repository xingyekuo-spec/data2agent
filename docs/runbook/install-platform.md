# 数据平台安装步骤

> 仅本机操作。**先完成本机并启动 ingest**,再让中间机推送。两台机使用**同一 Release 版本**。
> 详解见 [windows-deploy.md](windows-deploy.md)。本机不连 ERP,无需 ODBC。

1. 安装 **Python 3.14 官方 64 位**(勾选 Add to PATH)。

2. 拿到 Release 附件 `d2a-runtime-platform-<版本>.zip`(与中间机同版本),拷到本机。

3. 建目录与虚拟环境:
   ```powershell
   New-Item -ItemType Directory -Force C:\d2a\app, C:\d2a\data\logs, C:\d2a\config | Out-Null
   python -m venv C:\d2a\venv
   C:\d2a\venv\Scripts\python.exe -m pip install --upgrade pip
   ```

4. 解压并离线安装:
   ```powershell
   Expand-Archive d2a-runtime-platform-<版本>.zip C:\d2a\app
   C:\d2a\venv\Scripts\pip.exe install --no-index --find-links=C:\d2a\app\wheels -e C:\d2a\app[ingest,connect,mcp,console]
   ```

5. **管理员** PowerShell 生成配置并写入 token(按提示输入 ingest token;mcp/console token 自动生成并显示,请记下):
   ```powershell
   C:\d2a\app\setup-platform.ps1
   ```

6. **新开**普通 PowerShell 窗口(机器级环境变量对新进程生效)。

7. 先手动启动接收端(中间机推送依赖此进程):
   ```powershell
   C:\d2a\venv\Scripts\python.exe -m data2agent.ingest --landing C:\d2a\data\factory.sqlite --host 0.0.0.0 --port 8850
   ```

8. 本机健康检查(另开窗口):
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8850/ingest/health
   ```

9. 防火墙放行入站 **8850**(仅对中间机 IP)。中间机 `serve --once` 成功后,本机确认 raw 表:
   ```powershell
   C:\d2a\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'C:\d2a\data\factory.sqlite'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'\")])"
   ```

10. (可选)手工跑一轮物化:
    ```powershell
    C:\d2a\venv\Scripts\python.exe -m data2agent.connect apply --config C:\d2a\config\platform.yaml --landing C:\d2a\data\factory.sqlite
    ```

11. (可选)用 NSSM 装四个常驻服务(先停第 7 步前台进程):
    ```powershell
    # d2a-ingest
    C:\d2a\nssm\nssm.exe install d2a-ingest C:\d2a\venv\Scripts\python.exe
    C:\d2a\nssm\nssm.exe set d2a-ingest AppParameters "-m data2agent.ingest --landing C:\d2a\data\factory.sqlite --host 0.0.0.0 --port 8850"
    C:\d2a\nssm\nssm.exe set d2a-ingest AppDirectory C:\d2a\app
    C:\d2a\nssm\nssm.exe set d2a-ingest AppStdout C:\d2a\data\logs\d2a-ingest.log
    C:\d2a\nssm\nssm.exe set d2a-ingest AppStderr C:\d2a\data\logs\d2a-ingest.log
    C:\d2a\nssm\nssm.exe set d2a-ingest AppExit Default Restart

    # d2a-apply
    C:\d2a\nssm\nssm.exe install d2a-apply C:\d2a\venv\Scripts\python.exe
    C:\d2a\nssm\nssm.exe set d2a-apply AppParameters "-m data2agent.connect apply --config C:\d2a\config\platform.yaml --landing C:\d2a\data\factory.sqlite --every 1800"
    C:\d2a\nssm\nssm.exe set d2a-apply AppDirectory C:\d2a\app
    C:\d2a\nssm\nssm.exe set d2a-apply AppStdout C:\d2a\data\logs\d2a-apply.log
    C:\d2a\nssm\nssm.exe set d2a-apply AppStderr C:\d2a\data\logs\d2a-apply.log
    C:\d2a\nssm\nssm.exe set d2a-apply AppExit Default Restart

    # d2a-mcp
    C:\d2a\nssm\nssm.exe install d2a-mcp C:\d2a\venv\Scripts\python.exe
    C:\d2a\nssm\nssm.exe set d2a-mcp AppParameters "-m data2agent.mcp_server --db C:\d2a\data\factory.sqlite --transport http --host 0.0.0.0 --port 8848"
    C:\d2a\nssm\nssm.exe set d2a-mcp AppDirectory C:\d2a\app
    C:\d2a\nssm\nssm.exe set d2a-mcp AppStdout C:\d2a\data\logs\d2a-mcp.log
    C:\d2a\nssm\nssm.exe set d2a-mcp AppStderr C:\d2a\data\logs\d2a-mcp.log
    C:\d2a\nssm\nssm.exe set d2a-mcp AppExit Default Restart

    # d2a-console
    C:\d2a\nssm\nssm.exe install d2a-console C:\d2a\venv\Scripts\python.exe
    C:\d2a\nssm\nssm.exe set d2a-console AppParameters "-m data2agent.console --config C:\d2a\config\platform.yaml --host 0.0.0.0 --port 8849"
    C:\d2a\nssm\nssm.exe set d2a-console AppDirectory C:\d2a\app
    C:\d2a\nssm\nssm.exe set d2a-console AppStdout C:\d2a\data\logs\d2a-console.log
    C:\d2a\nssm\nssm.exe set d2a-console AppStderr C:\d2a\data\logs\d2a-console.log
    C:\d2a\nssm\nssm.exe set d2a-console AppExit Default Restart

    C:\d2a\nssm\nssm.exe start d2a-ingest
    C:\d2a\nssm\nssm.exe start d2a-apply
    C:\d2a\nssm\nssm.exe start d2a-mcp
    C:\d2a\nssm\nssm.exe start d2a-console
    ```
