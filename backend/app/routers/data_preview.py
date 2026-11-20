"""数据预览接口：维护预览模板，覆盖提交校验、启用模板、停用模板等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.data_preview import DataPreviewService

router = APIRouter(prefix="/api/data_preview", tags=["数据预览"])

service = DataPreviewService()

LIST_FIELDS = ["模板编号", "数据源名", "预查询SQL", "返回列数", "采样行数", "缓存秒数", "刷新策略", "模板版本"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、模板编号关键字与状态过滤数据预览列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="data_preview.csv"'})


@router.get("/{entry_id}/sample-params", response_model=dict)
def describe_sample_params(entry_id: int) -> dict:
    """读取取样所需参数声明：关联SQL模板的参数、类型与模板默认值。"""
    result = service.describe_sample_params(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "预览模板不存在"))
    return result


@router.post("/{entry_id}/samples", response_model=dict)
def preview_samples(entry_id: int, payload: dict[str, object]) -> dict:
    """执行数据预览取样。

    入参 {"values": {...}}。先产出模板参数绑定结果，绑定失败
    （缺参/空集合/类型不符）时返回 422 并指出字段，不执行查询；
    绑定成功才取样，零行结果以 status=executed_zero_rows 正常返回。
    """
    values = payload.get("values", {})
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values 必须是参数名到参数值的对象")
    result = service.preview_samples(entry_id, values)
    if not result.get("ok"):
        # 绑定校验失败属于入参不可执行（422），与查询零行（200）明确区分
        raise HTTPException(status_code=422, detail=result)
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预览模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预览模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条预览模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预览模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条预览模板执行提交校验、启用模板、停用模板；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
