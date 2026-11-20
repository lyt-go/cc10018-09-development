"""数据预览业务规则：状态流转、字段校验与筛选口径都收在这里。

取样能力（preview_samples）放在本服务内编排，但实际执行委托给
engine/preview_sampler.py：先产出模板参数绑定结果，绑定通过才允许取样。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.engine.preview_sampler import PreviewSampler

MODULE = "data_preview"
LIST_FIELDS = ["模板编号", "数据源名", "预查询SQL", "返回列数", "采样行数", "缓存秒数", "刷新策略", "模板版本"]
REQUIRED_FIELDS = ["模板编号", "数据源名", "预查询SQL"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用模板": "已启用", "停用模板": "已停用"}
NEGATIVE_ACTIONS = ["停用模板"]


class DataPreviewService:
    def __init__(self) -> None:
        self.sampler = PreviewSampler()

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

    def describe_sample_params(self, entry_id: int) -> dict[str, Any]:
        """取样表单：返回关联SQL模板声明的参数与模板默认值。"""
        return self.sampler.describe_params(entry_id)

    def preview_samples(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """基于模板参数绑定结果执行数据预览取样。

        绑定失败时返回 status=binding_failed 并携带 field_errors，
        调用方应在取样前提示问题字段；零行结果为 executed_zero_rows。
        """
        return self.sampler.sample(entry_id, values or {})

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预览模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据预览可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"预览模板已{action}"
