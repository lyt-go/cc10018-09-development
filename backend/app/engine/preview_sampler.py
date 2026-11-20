"""数据预览取样引擎：先绑定、后取样，样本来源与模板参数绑定结果强关联。

执行纪律：
1. 预览模板必须能关联到 SQL 模板，绑定结果（bindings/bound_sql/params）随样本一起返回；
2. 绑定校验失败（缺参、空集合、类型不符）时在取样前直接指出问题字段，
   既不调用查询优化，也不执行取样，避免错误绑定被优化后照常出样本；
3. 绑定成功才执行（模拟）查询，并区分两种空态：
   - executed_zero_rows：条件合法、查询成功但零行；
   - binding_failed：入参缺失或未达到可执行条件，根本没有执行查询。
"""
from __future__ import annotations

import re
from typing import Any

from app.store import store
from app.engine.sql_binder import (
    BINDING_MISSING,
    BINDING_EMPTY,
    BINDING_TYPE,
    BINDING_TEMPLATE,
    SqlTemplateBinder,
)
from app.engine.query_optimizer import QueryOptimizer

# 取样结果状态（报表团队据此区分零行空态与入参不可执行）
SAMPLE_OK = "executed"
SAMPLE_ZERO_ROWS = "executed_zero_rows"
SAMPLE_BINDING_FAILED = "binding_failed"
SAMPLE_PREVIEW_MISSING = "preview_not_found"
SAMPLE_TEMPLATE_MISSING = "template_not_linked"

# 内存样本表：键为预览模板声明的数据源名，模拟真实数据源中的取样数据
SAMPLE_TABLES: dict[str, list[dict[str, Any]]] = {
    "dwd_trade_order_di": [
        {"dt": "2026-11-14", "shop_id": "101", "order_amt": 1280.50},
        {"dt": "2026-11-15", "shop_id": "101", "order_amt": 2360.00},
        {"dt": "2026-11-15", "shop_id": "102", "order_amt": 980.20},
        {"dt": "2026-11-15", "shop_id": "103", "order_amt": 1750.80},
        {"dt": "2026-11-16", "shop_id": "102", "order_amt": 640.00},
        {"dt": "2026-11-10", "shop_id": "101", "order_amt": 320.10},
    ],
    "dwd_user_active_di": [
        {"dt": "2026-11-15", "user_id": "u001", "duration_sec": 120},
        {"dt": "2026-11-15", "user_id": "u002", "duration_sec": 45},
        {"dt": "2026-11-15", "user_id": "u003", "duration_sec": 320},
        {"dt": "2026-11-14", "user_id": "u004", "duration_sec": 90},
    ],
    "dwd_item_view_di": [
        {"dt": "2026-11-15", "category_id": "c01", "click_cnt": 120, "impression_cnt": 2000},
        {"dt": "2026-11-15", "category_id": "c02", "click_cnt": 80, "impression_cnt": 1600},
        {"dt": "2026-11-14", "category_id": "c01", "click_cnt": 60, "impression_cnt": 1500},
    ],
}

# 列表参数名 → 样本表过滤列
LIST_COLUMN_MAP = {
    "shop_list": "shop_id",
    "cat_list": "category_id",
}


class PreviewSampler:
    """数据预览取样器：绑定结果是取样的唯一入口。"""

    def __init__(self) -> None:
        self.binder = SqlTemplateBinder()
        self.optimizer = QueryOptimizer()

    # ───────────────────────────────────────────────
    # 关联解析
    # ───────────────────────────────────────────────
    def resolve_linked_template(self, preview: dict[str, Any]) -> dict[str, Any] | None:
        """预览模板 → API 接口 → SQL 模板，找到取样所依据的模板定义。"""
        api_code = preview.get("模板编号", "")
        api = next(
            (r for r in store.rows("api_service") if r.get("接口编号") == api_code),
            None,
        )
        if not api:
            return None
        tmpl_code = api.get("参数列表", "")
        if not tmpl_code.startswith("SQL-"):
            return None
        return self.binder.find_template_by_code(tmpl_code)

    def describe_params(self, preview_id: int) -> dict[str, Any]:
        """返回取样表单所需的参数声明（名称、类型、模板默认值）。"""
        preview = store.find("data_preview", preview_id)
        if not preview:
            return {"ok": False, "status": SAMPLE_PREVIEW_MISSING,
                    "message": f"预览模板 {preview_id} 不存在或已归档"}
        tmpl = self.resolve_linked_template(preview)
        if not tmpl:
            return {"ok": False, "status": SAMPLE_TEMPLATE_MISSING,
                    "message": "预览模板未关联可用的SQL模板，无法声明取样参数"}
        sql_text = tmpl.get("SQL片段", "")
        defaults = self.binder._template_defaults(tmpl)
        params = []
        for p in self.binder.parse_params(sql_text):
            name = p["name"]
            params.append({
                "name": name,
                "type": p["type"],
                "default": defaults.get(name, ""),
                "has_default": name in defaults,
            })
        return {
            "ok": True,
            "preview_id": preview_id,
            "template_code": tmpl.get("模板编号", ""),
            "template_id": tmpl.get("id"),
            "sample_limit": int(preview.get("采样行数") or 100),
            "params": params,
        }

    # ───────────────────────────────────────────────
    # 取样主流程
    # ───────────────────────────────────────────────
    def sample(self, preview_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """关联绑定结果后再执行取样；绑定失败绝不进入查询/优化环节。"""
        preview = store.find("data_preview", preview_id)
        if not preview:
            return {
                "ok": False,
                "executable": False,
                "status": SAMPLE_PREVIEW_MISSING,
                "empty_state": True,
                "empty_reason": "preview_missing",
                "message": f"预览模板 {preview_id} 不存在或已归档",
                "samples": [],
            }

        tmpl = self.resolve_linked_template(preview)
        if not tmpl:
            return {
                "ok": False,
                "executable": False,
                "status": SAMPLE_TEMPLATE_MISSING,
                "empty_state": True,
                "empty_reason": "template_not_linked",
                "message": "预览模板未关联可用的SQL模板，未达到可执行条件",
                "preview_id": preview_id,
                "samples": [],
            }

        sql_text = tmpl.get("SQL片段", "")
        # 样本来源与模板参数绑定结果关联：同一个模板、同一份绑定产物
        binding = self.binder.build_binding(sql_text, values, template=tmpl)
        if not binding["executable"]:
            # 校验失败：取样前指出字段，不做优化、不做查询
            return {
                "ok": False,
                "executable": False,
                "status": SAMPLE_BINDING_FAILED,
                "binding_status": binding["status"],
                "empty_state": True,
                "empty_reason": self._map_binding_reason(binding["status"]),
                "message": "参数绑定校验失败，已在取样前拦截，未执行查询",
                "preview_id": preview_id,
                "sample_source": {
                    "preview_id": preview_id,
                    "template_code": tmpl.get("模板编号", ""),
                    "datasource": preview.get("数据源名", ""),
                },
                "binding": binding,
                "field_errors": binding["field_errors"],
                "missing_fields": binding["missing_fields"],
                "empty_fields": binding["empty_fields"],
                "samples": [],
            }

        # 绑定可执行后才允许查询优化介入
        optimization = self.optimizer.analyze_sql(binding["sql"])

        # 执行（模拟）查询：严格使用展开后的绑定条件
        rows = self._execute(preview, binding)
        limit = self._extract_limit(binding["sql"], int(preview.get("采样行数") or 100))
        samples = rows[:limit]
        zero_rows = len(samples) == 0

        return {
            "ok": True,
            "executable": True,
            "status": SAMPLE_ZERO_ROWS if zero_rows else SAMPLE_OK,
            "empty_state": zero_rows,
            "empty_reason": "zero_rows" if zero_rows else None,
            "message": "查询执行成功，返回零行空态" if zero_rows else f"取样成功，共 {len(samples)} 行",
            "preview_id": preview_id,
            "sample_source": {
                "preview_id": preview_id,
                "template_code": tmpl.get("模板编号", ""),
                "datasource": preview.get("数据源名", ""),
            },
            "binding": binding,
            "bound_sql": binding["sql"],
            "params": binding["params"],
            "applied_conditions": self._describe_conditions(binding),
            "optimization": {
                "estimated_cost": optimization.get("estimated_cost"),
                "index_suggestions": optimization.get("index_suggestions", []),
            },
            "sample_limit": limit,
            "row_count": len(samples),
            "samples": samples,
        }

    # ───────────────────────────────────────────────
    # 内部：模拟查询执行
    # ───────────────────────────────────────────────
    def _execute(self, preview: dict[str, Any], binding: dict[str, Any]) -> list[dict[str, Any]]:
        table = SAMPLE_TABLES.get(preview.get("数据源名", ""), [])
        conditions = self._build_filters(binding)
        rows = []
        for row in table:
            if all(predicate(row) for predicate in conditions):
                rows.append(dict(row))
        return rows

    def _build_filters(self, binding: dict[str, Any]) -> list[Any]:
        predicates: list[Any] = []
        by_field = {b["field"]: b for b in binding["bindings"]}

        # 日期区间：start_date / end_date 成对出现
        if "start_date" in by_field and "end_date" in by_field:
            start = by_field["start_date"]["value"]
            end = by_field["end_date"]["value"]
            predicates.append(lambda row, s=start, e=end: s <= str(row.get("dt", "")) <= e)
        elif "query_date" in by_field:
            day = by_field["query_date"]["value"]
            predicates.append(lambda row, d=day: str(row.get("dt", "")) == d)

        # 列表 IN 条件：每个列表项都已对应到展开后的占位符
        for field, column in LIST_COLUMN_MAP.items():
            if field in by_field:
                allowed = set(by_field[field]["value"])
                predicates.append(lambda row, col=column, allow=allowed: str(row.get(col, "")) in allow)
        return predicates

    def _describe_conditions(self, binding: dict[str, Any]) -> list[str]:
        lines = []
        for b in binding["bindings"]:
            if b["type"] == "list":
                lines.append(
                    f"{b['field']} IN ({', '.join(b['placeholders'])})，"
                    f"{b['item_count']} 项，来源={b['source']}，值={b['value']}"
                )
            else:
                lines.append(
                    f"{b['field']} = {b['placeholder']}，来源={b['source']}，值={b['value']}"
                )
        return lines

    @staticmethod
    def _extract_limit(sql: str, default: int) -> int:
        m = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
        return int(m.group(1)) if m else default

    @staticmethod
    def _map_binding_reason(status: str) -> str:
        return {
            BINDING_MISSING: "missing_param",
            BINDING_EMPTY: "empty_collection",
            BINDING_TYPE: "type_mismatch",
            BINDING_TEMPLATE: "template_error",
        }.get(status, "binding_failed")
