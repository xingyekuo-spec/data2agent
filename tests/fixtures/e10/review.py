"""接单评审链路辅助(测试 fixture，非产品入口)。"""

from __future__ import annotations

from data2agent.mcp_server.core import QueryService


def build_review(svc: QueryService, customer: str, keyword: str,
                 qty: float, target_price: float) -> dict:
    """参考链主体:三次取数 + 决策规则 + 建议卡。返回 propose_action 的卡片。"""
    # 1. 客户档案(账期 / 区域 / 币别;联系方式自动脱敏)
    cust_res = svc.query_objects("Customer", filters={"customer_code": customer})
    if not cust_res["rows"]:
        raise SystemExit(f"客户 {customer} 不存在(试试 query_objects Customer 目录)")
    cust = cust_res["rows"][0]
    q_cust = cust_res["meta"]["query_id"]
    d_cust = cust_res["meta"]["result_digest"]

    # 2. 该客户历史报价,按型谱关键词收敛(Agent 式的客户端匹配)
    quotes_res = svc.query_objects("Quotation", filters={"customer": customer}, limit=200)
    q_quotes = quotes_res["meta"]["query_id"]
    d_quotes = quotes_res["meta"]["result_digest"]
    similar = [r for r in quotes_res["rows"] if keyword in (r["spec_summary"] or "")]
    won = sorted((r for r in similar if r["result"] == "成交"),
                 key=lambda r: r["quote_date"], reverse=True)

    # 3. 毛利率基线(按客户,CNY 口径)
    margin_res = svc.query_metrics("gross_margin_rate", group_by="客户", limit=200)
    q_margin = margin_res["meta"]["query_id"]
    d_margin = margin_res["meta"]["result_digest"]
    baseline = next((r["value"] for r in margin_res["rows"]
                     if str(r["group"]).startswith(customer)), None)

    # 4. 决策规则(参考版:真实规则属行业知识包)
    evidence = [{
        "claim": f"客户 {customer} {cust['name']}({cust['region']}),"
                 f"账期 {cust['payment_days']} 天,结算币别 {cust['currency']}",
        "query_id": q_cust,
        "result_digest": d_cust,
    }]
    risks = []
    if cust["payment_days"] >= 90:
        risks.append(f"账期 {cust['payment_days']} 天偏长,资金占用风险")

    if won:
        ref = won[0]
        evidence.append({
            "claim": f"同型谱『{keyword}』历史报价 {len(similar)} 笔、成交 {len(won)} 笔;"
                     f"最近成交 {ref['quote_no']}({ref['quote_date']}):"
                     f"{ref['quoted_price']} {ref['currency']}",
            "query_id": q_quotes,
            "result_digest": d_quotes,
        })
        discount = 1 - target_price / ref["quoted_price"]
        if baseline is not None:
            evidence.append({
                "claim": f"该客户历史毛利率 {baseline:.1%}(CNY 口径)",
                "query_id": q_margin,
                "result_digest": d_margin,
            })
            est = baseline - discount
            if est >= 0.25:
                verdict = "接"
            elif est >= 0.15:
                verdict = "谨慎接"
            else:
                verdict = "不接"
            price_note = (f"目标价 {target_price} 较最近成交价低 {discount:.1%},"
                          f"粗估毛利 ~{est:.1%}" if discount > 0 else
                          f"目标价 {target_price} 不低于最近成交价,毛利有保障")
            counter = (f";建议还价至 ≥{ref['quoted_price'] * (1 - max(baseline - 0.2, 0)):.2f} "
                       f"{ref['currency']}" if verdict != "接" and discount > 0 else "")
            conclusion = f"{verdict} —— {price_note}" + (
                f",叠加{'、'.join(risks)}" if risks else "") + counter
        else:
            conclusion = (f"谨慎接 —— 有同型谱成交历史({ref['quoted_price']} "
                          f"{ref['currency']}),但缺该客户毛利基线,需成本核算后定价")
    else:
        conclusion = (f"谨慎接 —— 客户 {customer} 无『{keyword}』型谱成交历史,"
                      "属新品类询单,需工艺与成本核算后再报价"
                      + (f";注意{'、'.join(risks)}" if risks else ""))

    return svc.propose_action("Quotation", "quote_review", conclusion, evidence)


def render_card(card: dict, inquiry: str) -> str:
    lines = [
        f"┌─ 接单评审建议卡 {card['proposal_id']} · {card['at']} " + "─" * 20,
        f"│ 询单:{inquiry}",
        f"│ 动作:{card['action']}({card['action_desc']})  档位:{card['tier']}",
        "│",
        f"│ 结论:{card['conclusion']}",
        "│ 依据(每条可溯源):",
    ]
    for i, ev in enumerate(card["evidence"], 1):
        q = ev["query"]
        at = q.get("created_at") or q.get("at") or ""
        lines.append(f"│   {i}. {ev['claim']}")
        lines.append(f"│      ↳ [{q['query_id']}] {q['tool']}({q['target']}) @ {at}")
    if card["caveats"]:
        lines.append("│ 口径警示:")
        lines += [f"│   - {c}" for c in card["caveats"]]
    lines.append(f"│ 治理:{card['governance']}")
    lines.append("└" + "─" * 60)
    return "\n".join(lines)

