"""平台侧接收端点(§12.3):收中间服务器推来的 raw 批次与表完成事件。

Pattern A 的平台侧入口 —— 中间的 HttpPushSink POST /ingest/batch,并在每张表
全部批次确认后 POST /ingest/table-complete(零行表也发送)。本服务复用
connect.landing 幂等落地，并持久化表级完成证据供 Validation 使用。
入口:python -m data2agent.ingest。
"""
