"""SQL模板参数绑定与数据预览取样测试。

覆盖验收要点：
- 缺省参数只回填本模板声明的默认值；
- 日期/整数类型校验，错误要指出具体字段；
- 列表逐项对应占位符，显式空列表不能当缺省扩大查询；
- 数据预览先关联绑定结果再取样，校验失败在取样前拦截；
- 零行空态（executed_zero_rows）与入参不可执行（binding_failed）可区分；
- 日期在前、多城市在后的模板，取样实际使用展开后的条件。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import store
from app.engine.sql_binder import (
    SqlTemplateBinder,
    BINDING_MISSING,
    BINDING_EMPTY,
    BINDING_TYPE,
)
from app.engine.preview_sampler import (
    PreviewSampler,
    SAMPLE_OK,
    SAMPLE_ZERO_ROWS,
    SAMPLE_BINDING_FAILED,
)

TRADE_SQL = (
    "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
    "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
    "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt"
)


def reset_store() -> None:
    store._tables = {}
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def test_parse_params_order_and_type() -> None:
    binder = SqlTemplateBinder()
    params = binder.parse_params(TRADE_SQL)
    assert [p["name"] for p in params] == ["start_date", "end_date", "shop_list"]
    assert [p["type"] for p in params] == ["date", "date", "list"]
    print("  [PASS] 参数按出现顺序解析且类型正确")


def test_defaults_only_from_own_template() -> None:
    reset_store()
    binder = SqlTemplateBinder()

    # 模板1（SQL-TRADE-001）声明了默认值：全部缺省时回填
    result = binder.bind_template(1, {})
    assert result["ok"], result
    assert result["params"] == ["2026-11-01", "2026-11-15", "101", "102"]
    assert {b["field"]: b["source"] for b in result["bindings"]} == {
        "start_date": "default",
        "end_date": "default",
        "shop_list": "default",
    }

    # 相同参数名但 SQL 不属于任何模板：不允许借用别的模板默认值
    other = binder.bind(
        "SELECT * FROM other WHERE dt = {{query_date:date}} AND id IN {{shop_list:list}}",
        {},
    )
    assert not other["ok"]
    assert other["status"] == BINDING_MISSING
    assert set(other["missing_fields"]) == {"query_date", "shop_list"}
    print("  [PASS] 缺省参数只使用本模板声明的默认值")


def test_explicit_empty_list_is_not_default_and_blocks() -> None:
    reset_store()
    binder = SqlTemplateBinder()

    # 即使模板声明了 shop_list 默认值，显式空列表也必须报错而不是回填默认值
    result = binder.bind_template(1, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": [],
    })
    assert not result["ok"]
    assert result["status"] == BINDING_EMPTY
    assert result["empty_fields"] == ["shop_list"]
    assert result["sql"] is None and result["params"] == []
    assert "shop_list" in result["field_errors"]

    # 空字符串等价于空集合
    result_str = binder.bind_template(1, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": "",
    })
    assert not result_str["ok"] and result_str["status"] == BINDING_EMPTY
    print("  [PASS] 显式空列表不按缺省处理，必填条件拒绝空集合")


def test_date_and_int_validation_points_field() -> None:
    reset_store()
    binder = SqlTemplateBinder()

    result = binder.bind(
        "SELECT * FROM t WHERE dt={{d:date}} AND n={{n:int}}",
        {"d": "2026-02-31", "n": "12x"},
    )
    assert not result["ok"]
    assert result["status"] == BINDING_TYPE
    assert "d" in result["field_errors"] and "n" in result["field_errors"]
    assert "日期" in result["field_errors"]["d"]
    assert "整数" in result["field_errors"]["n"]

    # 格式正确但日历非法也要拦截
    bad_fmt = binder.bind("SELECT * FROM t WHERE dt={{d:date}}", {"d": "11/15/2026"})
    assert not bad_fmt["ok"] and bad_fmt["status"] == BINDING_TYPE

    # 合法整数转换为 int 类型
    ok = binder.bind("SELECT * FROM t WHERE n={{n:int}}", {"n": " -42 ".strip()})
    assert ok["ok"] and ok["params"] == [-42]
    print("  [PASS] 日期与整数校验失败按字段指出")


def test_list_items_map_to_placeholders_in_order() -> None:
    reset_store()
    binder = SqlTemplateBinder()

    result = binder.bind(
        TRADE_SQL,
        {"start_date": "2026-11-10", "end_date": "2026-11-20",
         "shop_list": ["101", "102", "103"]},
    )
    assert result["ok"]
    # 日期占位符在前，城市列表在后，编号连续不错位
    assert result["params"] == ["2026-11-10", "2026-11-20", "101", "102", "103"]
    assert "BETWEEN $1 AND $2" in result["sql"]
    assert "IN ($3, $4, $5)" in result["sql"]
    list_binding = next(b for b in result["bindings"] if b["field"] == "shop_list")
    assert list_binding["item_count"] == 3
    assert list_binding["placeholders"] == ["$3", "$4", "$5"]

    # 列表项不允许嵌套复合类型
    bad = binder.bind("SELECT * FROM t WHERE id IN {{ids:list}}", {"ids": [["1"]]})
    assert not bad["ok"] and bad["status"] == BINDING_TYPE
    print("  [PASS] 列表逐项对应正确占位符且顺序与模板一致")


def test_preview_happy_path_uses_expanded_conditions() -> None:
    reset_store()
    sampler = PreviewSampler()

    result = sampler.sample(1, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": ["101", "102"],
    })
    assert result["ok"]
    assert result["status"] == SAMPLE_OK
    # 样本来源关联到模板参数绑定结果
    assert result["sample_source"]["template_code"] == "SQL-TRADE-001"
    assert result["binding"]["executable"] is True
    assert result["bound_sql"].startswith("SELECT dt, shop_id")
    # 取样实际应用展开后的条件：只含 101/102 且在日期区间内
    assert result["row_count"] == 4
    for row in result["samples"]:
        assert row["shop_id"] in {"101", "102"}
        assert "2026-11-01" <= row["dt"] <= "2026-11-15"
    print("  [PASS] 预览样本基于展开后的绑定条件取样")


def test_preview_empty_city_blocked_before_sampling() -> None:
    reset_store()
    sampler = PreviewSampler()

    # 选好日期但清空城市：空集合与必填条件冲突，不得扫全城市
    result = sampler.sample(1, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": [],
    })
    assert not result["ok"]
    assert result["status"] == SAMPLE_BINDING_FAILED
    assert result["binding_status"] == BINDING_EMPTY
    assert result["empty_fields"] == ["shop_list"]
    assert "shop_list" in result["field_errors"]
    # 没有进入查询优化，也没有样本
    assert "optimization" not in result
    assert result["samples"] == []
    print("  [PASS] 清空城市在取样前拦截，不默认扫全城市")


def test_preview_bad_binding_reports_field_before_sample() -> None:
    reset_store()
    sampler = PreviewSampler()

    result = sampler.sample(1, {
        "start_date": "not-a-date",
        "end_date": "2026-11-15",
        "shop_list": ["101"],
    })
    assert not result["ok"]
    assert result["status"] == SAMPLE_BINDING_FAILED
    assert "start_date" in result["field_errors"]
    assert result["samples"] == []
    assert "optimization" not in result
    print("  [PASS] 校验失败的绑定在取样前指出字段")


def test_preview_zero_rows_distinct_from_missing_params() -> None:
    reset_store()
    sampler = PreviewSampler()

    # 条件合法但查不到数据：零行空态，HTTP 层面仍是成功执行
    zero = sampler.sample(1, {
        "start_date": "2020-01-01",
        "end_date": "2020-01-02",
        "shop_list": ["101"],
    })
    assert zero["ok"] is True
    assert zero["status"] == SAMPLE_ZERO_ROWS
    assert zero["empty_state"] is True
    assert zero["empty_reason"] == "zero_rows"
    assert zero["row_count"] == 0

    # 入参缺失：未达到可执行条件，状态必须不同
    missing = sampler.sample(1, {})
    # 模板1有完整默认值，因此改用未关联模板的预览（id=3 -> API-ITEM-001 无 list 参数但有日期默认）
    item = sampler.sample(3, {"start_date": "2026-11-01"})  # end_date 有默认值，应可执行
    assert item["ok"], item

    # 直接构造缺参场景：清掉模板默认值后取样
    tmpl = next(r for r in store.rows("sql_template") if r["id"] == 1)
    tmpl["默认值"] = ""
    blocked = sampler.sample(1, {"start_date": "2026-11-01"})
    assert not blocked["ok"]
    assert blocked["status"] == SAMPLE_BINDING_FAILED
    assert blocked["empty_reason"] == "missing_param"
    assert set(blocked["missing_fields"]) == {"end_date", "shop_list"}
    assert blocked["empty_state"] is True and blocked["samples"] == []
    print("  [PASS] 零行空态与入参缺失/不可执行明确区分")


def test_preview_multi_city_expansion_after_dates() -> None:
    reset_store()
    sampler = PreviewSampler()

    # 先日期、后多个城市的模板，确认取样使用展开后的 IN 条件
    result = sampler.sample(1, {
        "start_date": "2026-11-15",
        "end_date": "2026-11-15",
        "shop_list": ["101", "102", "103"],
    })
    assert result["ok"]
    assert result["binding"]["params"] == ["2026-11-15", "2026-11-15", "101", "102", "103"]
    shops = {row["shop_id"] for row in result["samples"]}
    assert shops == {"101", "102", "103"}
    assert all(row["dt"] == "2026-11-15" for row in result["samples"])
    # 条件描述里能看到列表展开项数
    cond = next(c for c in result["applied_conditions"] if c.startswith("shop_list"))
    assert "3 项" in cond
    print("  [PASS] 日期在前多城市在后：取样使用展开后的条件")


def test_http_endpoints() -> None:
    reset_store()
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # 绑定参数声明
    r = client.get("/api/sql_template/1/binding-params")
    assert r.status_code == 200
    body = r.json()
    assert body["template_code"] == "SQL-TRADE-001"
    assert [p["name"] for p in body["params"]] == ["start_date", "end_date", "shop_list"]

    # 绑定成功
    r = client.post("/api/sql_template/1/binding", json={"values": {
        "start_date": "2026-11-01", "end_date": "2026-11-15", "shop_list": ["101"]}})
    assert r.status_code == 200 and r.json()["ok"] is True

    # 绑定失败：空集合
    r = client.post("/api/sql_template/1/binding", json={"values": {
        "start_date": "2026-11-01", "end_date": "2026-11-15", "shop_list": []}})
    body = r.json()
    assert body["ok"] is False and body["status"] == BINDING_EMPTY

    # 预览取样参数声明
    r = client.get("/api/data_preview/1/sample-params")
    assert r.status_code == 200
    assert r.json()["template_code"] == "SQL-TRADE-001"

    # 预览取样成功（零行也是 200）
    r = client.post("/api/data_preview/1/samples", json={"values": {
        "start_date": "2020-01-01", "end_date": "2020-01-02", "shop_list": ["101"]}})
    assert r.status_code == 200
    assert r.json()["status"] == SAMPLE_ZERO_ROWS

    # 预览绑定失败：422 且带字段
    r = client.post("/api/data_preview/1/samples", json={"values": {
        "start_date": "2026-11-01", "end_date": "2026-11-15", "shop_list": []}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["status"] == SAMPLE_BINDING_FAILED
    assert "shop_list" in detail["field_errors"]

    # 查询服务链：绑定失败时链路在SQL模板步骤终止
    r = client.post("/api/chain/query-service", json={
        "api_id": 1, "template_id": 1,
        "params": {"start_date": "2026-11-01", "end_date": "2026-11-15", "shop_list": []},
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["steps"][-1]["module"] == "sql_template"
    assert detail["binding"]["status"] == BINDING_EMPTY
    print("  [PASS] HTTP 端点：绑定、取样、422 与链路拦截")


def run_all() -> bool:
    tests = [
        test_parse_params_order_and_type,
        test_defaults_only_from_own_template,
        test_explicit_empty_list_is_not_default_and_blocks,
        test_date_and_int_validation_points_field,
        test_list_items_map_to_placeholders_in_order,
        test_preview_happy_path_uses_expanded_conditions,
        test_preview_empty_city_blocked_before_sampling,
        test_preview_bad_binding_reports_field_before_sample,
        test_preview_zero_rows_distinct_from_missing_params,
        test_preview_multi_city_expansion_after_dates,
        test_http_endpoints,
    ]
    print("\n=== SQL绑定与数据预览专项测试 ===")
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {t.__name__}: {exc}")
            failed += 1
    print(f"\n=== 专项测试结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 项 ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
