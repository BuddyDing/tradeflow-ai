"""Tools that let an agent inspect and analyze user-imported tabular data."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from tradeflow.tools.base import tool
from web.database import connect


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _txn_kind(value: Any) -> str:
    """把交易类型值归一到 order/refund/other，兼容中英文报表（Order/订单、Refund/退款）。"""
    s = str(value or "").strip().lower()
    if s in ("order", "订单", "shipment", "销售", "销售订单"):
        return "order"
    if "refund" in s or "退款" in s or "退货" in s:
        return "refund"
    return "other"


def _select_batch(user_id: str, store_id: str, batch_id: str = "",
                  report_type: str = ""):
    where = ["user_id=?", "store_id=?", "status='completed'", "row_count>0"]
    params: List[Any] = [user_id, store_id]
    if batch_id:
        where.append("id=?")
        params.append(batch_id)
    if report_type:
        where.append("report_type=?")
        params.append(report_type)
    with connect() as db:
        row = db.execute(
            "SELECT id,filename,report_type,row_count,columns_json,mapping_json,created_at "
            f"FROM import_batches WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


def _rows(user_id: str, store_id: str, batch_id: str, limit: int = 5000) -> List[Dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT row_json FROM imported_rows WHERE user_id=? AND store_id=? AND batch_id=? LIMIT ?",
            (user_id, store_id, batch_id, limit),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _matches(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    for key, expected in (filters or {}).items():
        actual = row.get(key)
        if isinstance(expected, list):
            if actual not in expected and str(actual) not in {str(x) for x in expected}:
                return False
        elif isinstance(expected, dict):
            value = _num(actual)
            if value is None:
                return False
            if "min" in expected and value < float(expected["min"]):
                return False
            if "max" in expected and value > float(expected["max"]):
                return False
            if "contains" in expected and str(expected["contains"]).lower() not in str(actual).lower():
                return False
        elif str(expected).lower() not in str(actual).lower():
            return False
    return True


def _field_name(batch: Dict[str, Any], requested: str) -> str:
    if not requested:
        return requested
    mapping = json.loads(batch.get("mapping_json") or "{}")
    return mapping.get(requested, requested)


def _normalize_filters(batch: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
    return {_field_name(batch, key): value for key, value in (filters or {}).items()}


def _invalid_group_value(field: str, value: Any) -> bool:
    if value in (None, ""):
        return True
    text_id_fields = {"asin", "sku", "search_term", "campaign", "order_id"}
    return field in text_id_fields and _num(value) is not None


def build_import_tools(user_id: str, store_id: str):
    @tool
    def list_imported_files() -> dict:
        """列出当前店铺已经导入的文件。先用它确认有哪些文件、类型、行数和 batch_id。"""
        with connect() as db:
            rows = db.execute(
                "SELECT id,filename,report_type,row_count,created_at,columns_json,mapping_json "
                "FROM import_batches WHERE user_id=? AND store_id=? AND status='completed' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 20",
                (user_id, store_id),
            ).fetchall()
        items = []
        for row in rows:
            d = dict(row)
            columns = json.loads(d.pop("columns_json") or "[]")
            mapping = json.loads(d.pop("mapping_json") or "{}")
            d["columns"] = columns[:40]
            d["mapped_fields"] = sorted(set(mapping.values()))
            items.append(d)
        return {"items": items}

    @tool
    def inspect_imported_file(batch_id: str = "", report_type: str = "") -> dict:
        """查看一个导入文件的字段、样例行和数字列概览。batch_id 优先；也可用 report_type 取最新同类型文件。"""
        batch = _select_batch(user_id, store_id, batch_id, report_type)
        if not batch:
            return {"error": "没有找到匹配的已导入文件"}
        data = _rows(user_id, store_id, batch["id"], 1000)
        columns = json.loads(batch.get("columns_json") or "[]")
        numeric: Dict[str, Dict[str, float]] = {}
        keys = sorted({k for row in data[:200] for k in row})
        for key in keys:
            vals = [v for row in data[:1000] if (v := _num(row.get(key))) is not None]
            if vals:
                numeric[key] = {
                    "count": len(vals),
                    "sum": round(sum(vals), 4),
                    "avg": round(sum(vals) / len(vals), 4),
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                }
        return {
            "batch": {k: batch[k] for k in ("id", "filename", "report_type", "row_count", "created_at")},
            "columns": columns or keys,
            "sample_rows": data[:5],
            "numeric_summary": numeric,
        }

    @tool
    def sample_imported_rows(batch_id: str = "", report_type: str = "",
                             filters: dict = None,
                             limit: int = 20) -> dict:
        """按条件抽取导入文件明细行。filters 支持 {列名:值}、{列名:{contains/min/max}}。"""
        batch = _select_batch(user_id, store_id, batch_id, report_type)
        if not batch:
            return {"error": "没有找到匹配的已导入文件"}
        filters = _normalize_filters(batch, filters or {})
        data = [r for r in _rows(user_id, store_id, batch["id"], 5000) if _matches(r, filters)]
        return {"batch_id": batch["id"], "matched": len(data), "rows": data[:max(1, min(limit, 100))]}

    @tool
    def aggregate_imported_file(batch_id: str = "", report_type: str = "",
                                group_by: str = "", metrics: dict = None,
                                filters: dict = None,
                                sort_by: str = "", limit: int = 20) -> dict:
        """聚合分析导入文件。metrics 形如 {"spend":"sum","orders":"sum","acos":"ratio:spend/sales"}。"""
        batch = _select_batch(user_id, store_id, batch_id, report_type)
        if not batch:
            return {"error": "没有找到匹配的已导入文件"}
        filters = _normalize_filters(batch, filters or {})
        data = [r for r in _rows(user_id, store_id, batch["id"], 10000) if _matches(r, filters)]
        metrics = metrics or {}
        metrics = {_field_name(batch, col): op for col, op in metrics.items()}
        sort_by = _field_name(batch, sort_by)
        group_by = _field_name(batch, group_by)
        if not group_by:
            group_by = "__all__"
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in data:
            if group_by == "__all__":
                groups["全部"].append(row)
                continue
            value = row.get(group_by)
            if _invalid_group_value(group_by, value):
                continue
            groups[str(value)].append(row)
        out = []
        for name, rows in groups.items():
            item: Dict[str, Any] = {group_by if group_by != "__all__" else "group": name, "rows": len(rows)}
            for col, op in metrics.items():
                op = str(op)
                if op.startswith("ratio:") and "/" in op:
                    left, right = op.removeprefix("ratio:").split("/", 1)
                    a = sum(_num(r.get(left)) or 0 for r in rows)
                    b = sum(_num(r.get(right)) or 0 for r in rows)
                    item[col] = round(a / b, 4) if b else None
                    continue
                vals = [_num(r.get(col)) for r in rows]
                vals = [v for v in vals if v is not None]
                if op == "count":
                    item[col] = len([r for r in rows if r.get(col) not in (None, "")])
                elif not vals:
                    item[col] = None
                elif op == "avg":
                    item[col] = round(sum(vals) / len(vals), 4)
                elif op == "min":
                    item[col] = round(min(vals), 4)
                elif op == "max":
                    item[col] = round(max(vals), 4)
                else:
                    item[col] = round(sum(vals), 4)
            out.append(item)
        if sort_by:
            out.sort(key=lambda x: (x.get(sort_by) is not None, x.get(sort_by) or 0), reverse=True)
        return {"batch_id": batch["id"], "filename": batch["filename"], "groups": out[:max(1, min(limit, 200))]}

    @tool
    def analyze_transactions(report_type: str = "orders", top_n: int = 20,
                             high_refund_rate: float = 0.15,
                             low_margin_rate: float = 0.30) -> dict:
        """按 SKU 分析交易/结算报告（亚马逊「日期范围报告」），做商品交易诊断与盈亏定位。

        分 type（Order/Refund/Adjustment 等）统计：销量、销售额、平台费后到手金额、
        退款笔数与退款率、平台费（销售佣金+FBA），并标出高退款率 / 低毛利的问题商品。
        阈值可调：high_refund_rate 退款率红线，low_margin_rate 平台费后毛利率红线。

        重要口径：total 是「平台各项费用扣除后的到手金额」，不是净利润；在没有采购成本
        时不能当利润用。缺列或缺数据时对应指标返回 null，并在 data_gaps / next_steps 里
        说明还需要哪些数据才能得到更强结论。"""
        batch = _select_batch(user_id, store_id, "", report_type)
        if not batch:
            return {"error": f"没有找到 report_type={report_type} 的导入文件，"
                             f"请先到「数据导入」上传日期范围报告（交易/结算）。"}
        data = _rows(user_id, store_id, batch["id"], 100000)

        def col(row: Dict[str, Any], *names: str):
            low = {str(k).lower(): v for k, v in row.items()}
            for n in names:
                if n in low:
                    return low[n]
            return None

        # save_import 把命中别名的列重命名为标准字段（product sales→sales、total→
        # net_amount、type→txn_type…）；用户手动映射同理。取值时标准名与原始英文名都认，
        # 兼容「已自动识别 / 用户手动指认 / 旧数据未映射」三种情况。
        sample_keys = {str(k).lower() for r in data[:300] for k in r}
        has_total = ("net_amount" in sample_keys) or ("total" in sample_keys)
        has_sales = ("sales" in sample_keys) or ("product sales" in sample_keys)
        has_fees = any(k in sample_keys for k in ("selling_fees", "selling fees", "fba_fees", "fba fees"))

        agg: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"order_rows": 0, "qty": 0.0, "sales": 0.0, "net": 0.0,
                     "fees": 0.0, "refund_rows": 0, "refund_amt": 0.0,
                     "adj_rows": 0, "adj_amt": 0.0})
        for r in data:
            sku = str(col(r, "sku") or "").strip()
            if not sku:
                continue
            kind = _txn_kind(col(r, "txn_type", "type"))
            a = agg[sku]
            sales = _num(col(r, "sales", "product sales")) or 0.0
            total = _num(col(r, "net_amount", "total")) or 0.0
            qty = _num(col(r, "orders", "quantity")) or 0.0
            fees = (abs(_num(col(r, "selling_fees", "selling fees")) or 0.0)
                    + abs(_num(col(r, "fba_fees", "fba fees")) or 0.0))
            if kind == "order":
                a["order_rows"] += 1; a["qty"] += qty; a["sales"] += sales
                a["net"] += total; a["fees"] += fees
            elif kind == "refund":
                a["refund_rows"] += 1; a["refund_amt"] += total
            else:
                a["adj_rows"] += 1; a["adj_amt"] += total

        rows_out = []
        for sku, a in agg.items():
            orders = a["order_rows"]
            refund_rate = round(a["refund_rows"] / orders, 4) if orders else None
            margin = round(a["net"] / a["sales"], 4) if a["sales"] else None
            flags = []
            if refund_rate is not None and refund_rate >= high_refund_rate:
                flags.append(f"高退款率{refund_rate:.0%}")
            if margin is not None and margin < low_margin_rate:
                flags.append(f"平台费后毛利率仅{margin:.0%}")
            if a["adj_amt"] < 0:
                flags.append(f"存在负向调整/扣费{round(a['adj_amt'],2)}")
            rows_out.append({
                "SKU": sku, "订单行数": int(orders), "销量": int(a["qty"]),
                "销售额": round(a["sales"], 2), "平台费后到手": round(a["net"], 2),
                "平台费": round(a["fees"], 2),
                "退款笔数": int(a["refund_rows"]), "退款率": refund_rate,
                "退款金额": round(a["refund_amt"], 2),
                "调整/赔偿净额": round(a["adj_amt"], 2),
                "平台费后毛利率": margin, "问题标记": flags,
            })
        rows_out.sort(key=lambda x: -x["销售额"])
        problems = [r for r in rows_out if r["问题标记"]]

        tot_sales = round(sum(r["销售额"] for r in rows_out), 2)
        tot_net = round(sum(r["平台费后到手"] for r in rows_out), 2)
        tot_orders = sum(r["订单行数"] for r in rows_out)
        tot_refunds = sum(r["退款笔数"] for r in rows_out)

        data_gaps, next_steps = [], []
        if not has_sales:
            data_gaps.append("报表缺 product sales 列，无法按 SKU 算销售额。")
        if not has_total:
            data_gaps.append("报表缺 total 列，无法算平台费后到手。")
        if not has_fees:
            data_gaps.append("报表缺 selling/fba fees 列，平台费未计入。")
        # 无论如何都提示成本缺口——这是从「平台费后到手」到「真实净利」的关键一跳
        next_steps.append("上传采购+头程成本表后，可把「平台费后到手」换算成真实净利与真实盈亏 ACOS。")
        next_steps.append("上传广告搜索词报告后，可结合广告花费判断每个 SKU 的广告是否盈利、算真实 ACOS 盈亏线。")

        return {
            "report": {k: batch[k] for k in ("id", "filename", "report_type", "row_count", "created_at")},
            "口径说明": "total=平台各项费用扣除后到手，非净利；未含采购成本，不代表利润。",
            "SKU数": len(rows_out),
            "总览": {"总销售额": tot_sales, "平台费后到手合计": tot_net,
                    "订单行数": int(tot_orders), "退款笔数": int(tot_refunds),
                    "整体退款率": round(tot_refunds / tot_orders, 4) if tot_orders else None,
                    "整体平台费后毛利率": round(tot_net / tot_sales, 4) if tot_sales else None},
            "问题商品": problems[:top_n],
            "按SKU": rows_out[:top_n],
            "data_gaps": data_gaps,
            "next_steps": next_steps,
        }

    return [list_imported_files, inspect_imported_file, sample_imported_rows,
            aggregate_imported_file, analyze_transactions]
