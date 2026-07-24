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

    return [list_imported_files, inspect_imported_file, sample_imported_rows, aggregate_imported_file]
