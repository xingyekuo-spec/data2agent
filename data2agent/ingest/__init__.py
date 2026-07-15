"""平台侧接收端点(§12.3):收中间服务器推来的 raw 批次,落地本地库。

Pattern A 的平台侧入口 —— 中间的 HttpPushSink POST /ingest/batch,本服务
复用 connect.landing 幂等落地。抽取 / 增量 / 对账逻辑不在此,只做"收 + 落"。
入口:python -m data2agent.ingest。
"""
