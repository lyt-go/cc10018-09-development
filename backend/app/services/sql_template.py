"""SQL模板业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.engine.sql_binder import SqlTemplateBinder

MODULE = "sql_template"
LIST_FIELDS = ["模板编号", "模板名称", "SQL片段", "参数标记", "参数类型", "默认值", "使用次数", "模板版本"]
REQUIRED_FIELDS = ["模板编号", "模板名称", "SQL片段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class SqlTemplateService:
    def __init__(self) -> None:
        self.binder = SqlTemplateBinder()

    def describe_binding_params(self, entry_id: int) -> dict[str, Any]:
        """返回模板声明的参数、类型与本模板默认值，供绑定表单渲染。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"模板定义 {entry_id} 不存在或已归档"}
        defaults = self.binder._template_defaults(entry)
        params = []
        for p in self.binder.parse_params(entry.get("SQL片段", "")):
            name = p["name"]
            params.append({
                "name": name,
                "type": p["type"],
                "default": defaults.get(name, ""),
                "has_default": name in defaults,
            })
        return {
            "ok": True,
            "template_id": entry_id,
            "template_code": entry.get("模板编号", ""),
            "sql_text": entry.get("SQL片段", ""),
            "params": params,
        }

    def build_binding(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """生成模板参数绑定结果（含空列表拦截、默认值回填、日期/整数校验）。"""
        result = self.binder.bind_template(entry_id, values or {})
        return result

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("模板编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模板定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SQL模板可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"模板定义已{action}"
