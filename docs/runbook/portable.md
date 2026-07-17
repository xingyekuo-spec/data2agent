# 便携包部署(解压即用)

> U 盘拷贝一个 zip → 解压 → **双击 `data2agent.exe`**。  
> 无需系统 Python、无需 pip、无需固定 `C:\d2a`。每个包只有这一个入口。

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
  config\
  data\logs\
  README.txt
```

## 用法

1. **中间机**:先装 [ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)(微软 MSI,一次即可)。
2. 解压对应 zip(两台机**同版本**)。
3. 双击 `data2agent.exe`:
   - 首次:浏览器填配置
   - 之后:打开管理界面,并自动拉起后台服务  
     (中间机 connector;平台 ingest + apply + mcp)
   - 右下角托盘图标常驻:**打开管理界面** / **退出**(退出会停止本程序拉起的后台进程)
   - 若已在运行,再次双击只会重新打开管理界面,不会重复启动

## 注意

- 整夹搬迁可以;不要只拷走 `data2agent.exe`。
- 凭据在 `config\secrets.env`。
