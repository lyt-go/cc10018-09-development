"""SQL模板参数绑定引擎：占位符解析、类型校验、默认值回填、动态SQL生成。

支持的占位符语法：
- {{param_name}}        → 文本替换（自动转义）
- {{param_name:int}}    → 整数类型校验
- {{param_name:date}}   → 日期格式与日历有效性校验
- {{param_name:list}}   → IN 列表展开，每项对应一个独立占位符
- {{#if cond}}...{{/if}} → 条件分支

绑定语义（数据预览等消费方共同遵守）：
- 调用方「未提供」的参数（键不存在或值为 None）才允许回填默认值，
  且默认值只能取自当前模板自身声明的「默认值」，不允许跨模板借用。
- 调用方显式传入空列表 / 空字符串属于「已提供」，不会被当作缺省，
  必填的列表参数收到空集合时直接报绑定错误，避免 IN () 退化为全表扫描。
- 列表中的每一项都必须是非空标量并通过类型校验，展开后逐项对应 $n 占位符。
- 校验失败会带具体字段名返回，绑定结果不可执行，消费方不得据此取样。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store

# 统一的绑定结果状态码，方便报表团队区分「零行空态」与「入参不可执行」
BINDING_OK = "ok"                    # 绑定成功，SQL 可执行
BINDING_MISSING = "missing_param"    # 必填参数缺失且模板未声明默认值
BINDING_EMPTY = "empty_collection"   # 显式传入空集合，与必填条件冲突
BINDING_TYPE = "type_mismatch"       # 日期/整数等类型校验失败
BINDING_TEMPLATE = "template_error"  # 模板本身不存在或声明不合法


class SqlTemplateBinder:
    """SQL模板参数绑定引擎。"""

    TYPE_PATTERNS = {
        "int": r"^-?\d+$",
        "float": r"^-?\d+(\.\d+)?$",
        "date": r"^\d{4}-\d{2}-\d{2}$",
        "datetime": r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$",
        "str": r".*",
    }

    PLACEHOLDER_RE = re.compile(r"\{\{(\w+)(?::(\w+))?\}\}")
    LIST_RE = re.compile(r"\{\{(\w+):list\}\}")

    def __init__(self) -> None:
        pass

    # ───────────────────────────────────────────────
    # 模板与默认值
    # ───────────────────────────────────────────────
    def parse_params(self, sql_text: str) -> list[dict[str, str]]:
        """从SQL模板中按出现顺序提取所有参数标记（同名参数只登记一次）。"""
        found = []
        seen = set()
        for m in self.PLACEHOLDER_RE.finditer(sql_text):
            name, ptype = m.group(1), m.group(2) or "str"
            if name not in seen:
                seen.add(name)
                found.append({"name": name, "type": ptype, "pos": m.start()})
        return found

    def get_template(self, template_id: int) -> dict[str, Any] | None:
        """按主键读取模板定义。"""
        return store.find("sql_template", template_id)

    def find_template_by_code(self, template_code: str) -> dict[str, Any] | None:
        """按业务模板编号（如 SQL-TRADE-001）读取模板定义。"""
        return next(
            (r for r in store.rows("sql_template") if r.get("模板编号") == template_code),
            None,
        )

    def _find_template_by_sql(self, sql_text: str) -> dict[str, Any] | None:
        for r in store.rows("sql_template"):
            if r.get("SQL片段", "") == sql_text:
                return r
        return None

    def _parse_defaults(self, defaults_raw: str) -> dict[str, str]:
        """解析默认值串，如 'start_date=2026-01-01,end_date=2026-12-31,shop_list=101,102'。

        只按 '参数名=' 切分，列表参数值内部的逗号保留在同一个默认值里，
        避免把 shop_list 的多个默认元素误拆成多个参数。
        """
        result: dict[str, str] = {}
        pending_key: str | None = None
        for part in (defaults_raw or "").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                pending_key = key.strip()
                if pending_key:
                    result[pending_key] = value.strip()
            elif pending_key is not None and part.strip():
                # 列表默认值的后续元素：a=1,2,3 → {a: '1,2,3'}
                result[pending_key] = f"{result[pending_key]},{part.strip()}"
        return result

    def _template_defaults(self, template: dict[str, Any] | None) -> dict[str, str]:
        if not template:
            return {}
        return self._parse_defaults(template.get("默认值", ""))

    # ───────────────────────────────────────────────
    # 参数取值与校验
    # ───────────────────────────────────────────────
    @staticmethod
    def _is_provided(value: Any) -> bool:
        """显式提供：键存在且值不是 None。空列表/空字符串也算已提供。"""
        return value is not None

    def _resolve_value(
        self,
        name: str,
        ptype: str,
        values: dict[str, Any],
        defaults: dict[str, str],
    ) -> tuple[Any, str | None, str | None]:
        """解析单个参数的最终取值。

        返回 (取值, 错误状态码, 错误说明)。取值为 None 且无错误时表示仍未解析成功。
        取值来源记录在调用方的 binding 里：provided=调用方入参，default=模板默认值。
        """
        provided = name in values and self._is_provided(values.get(name))
        if provided:
            return values[name], None, None

        # 仅当调用方未提供时才允许回填，且只能用本模板声明的默认值
        if name in defaults and defaults[name] != "":
            default_raw = defaults[name]
            if ptype == "list":
                return [item.strip() for item in default_raw.split(",") if item.strip()], None, None
            return default_raw, None, None

        return None, BINDING_MISSING, f"参数 {name} 未提供，且模板未声明可用默认值"

    @staticmethod
    def _is_valid_calendar_date(text: str) -> bool:
        """正则通过后再做真实日历校验，拒绝 2026-02-31 这类假日期。"""
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _check_scalar(self, name: str, ptype: str, raw: Any) -> tuple[Any, str | None, str | None]:
        """校验并转换单个标量值。"""
        if isinstance(raw, (list, tuple, dict, set)):
            return None, BINDING_TYPE, f"参数 {name} 需要标量值，收到复合类型 {type(raw).__name__}"
        text = str(raw).strip()
        if text == "":
            return None, BINDING_TYPE, f"参数 {name} 值为空，不符合类型 {ptype}"

        if ptype == "int":
            if not re.match(self.TYPE_PATTERNS["int"], text):
                return None, BINDING_TYPE, f"参数字段 {name} 的值 '{text}' 不是合法整数"
            return int(text), None, None

        if ptype == "float":
            if not re.match(self.TYPE_PATTERNS["float"], text):
                return None, BINDING_TYPE, f"参数字段 {name} 的值 '{text}' 不是合法数字"
            return float(text), None, None

        if ptype == "date":
            if not re.match(self.TYPE_PATTERNS["date"], text) or not self._is_valid_calendar_date(text):
                return None, BINDING_TYPE, f"参数字段 {name} 的值 '{text}' 不是合法日期（要求 YYYY-MM-DD）"
            return text, None, None

        if ptype == "datetime":
            if not re.match(self.TYPE_PATTERNS["datetime"], text):
                return None, BINDING_TYPE, f"参数字段 {name} 的值 '{text}' 不是合法日期时间"
            return text, None, None

        # str 及其余类型原样透传
        return text, None, None

    def _normalize_list(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            # 逗号分隔字符串：显式空字符串归一为空列表，保持「空集合」语义
            return [item.strip() for item in value.split(",") if item.strip()]
        return [value]

    def validate_params(
        self,
        sql_text: str,
        values: dict[str, Any],
        *,
        template: dict[str, Any] | None = None,
    ) -> list[str]:
        """校验参数值是否符合模板声明的类型，返回可读错误说明列表。"""
        result = self.build_binding(sql_text, values, template=template)
        return list(result["field_errors"].values())

    # ───────────────────────────────────────────────
    # 绑定结果（数据预览等消费方使用的可执行产物）
    # ───────────────────────────────────────────────
    def build_binding(
        self,
        sql_text: str,
        values: dict[str, Any],
        *,
        template: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """生成模板参数绑定结果。

        结果字段：
        - ok / executable：绑定是否通过、SQL 是否可执行
        - status：ok / missing_param / empty_collection / type_mismatch / template_error
        - sql：展开占位符后的可执行 SQL（失败时为 None）
        - params：按 $1..$n 顺序排列的实际参数值
        - bindings：每个占位符一条绑定明细（字段、类型、来源、对应占位符编号）
        - field_errors：字段名 → 错误说明，校验失败时在取样前指出问题字段
        - missing_fields / empty_fields：便于报表团队区分空态成因
        """
        if template is None:
            template = self._find_template_by_sql(sql_text)
        defaults = self._template_defaults(template)
        declared = {p["name"]: p["type"] for p in self.parse_params(sql_text)}

        field_errors: dict[str, str] = {}
        missing_fields: list[str] = []
        empty_fields: list[str] = []
        bindings: list[dict[str, Any]] = []
        bound_params: list[Any] = []
        statuses: list[str] = []

        def fail(name: str, code: str, message: str) -> None:
            field_errors[name] = message
            statuses.append(code)
            if code == BINDING_MISSING:
                missing_fields.append(name)
            elif code == BINDING_EMPTY:
                empty_fields.append(name)

        # 先对每个声明参数做一次取值+类型校验，保证所有问题字段一次性暴露
        resolved: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for name, ptype in declared.items():
            raw, code, message = self._resolve_value(name, ptype, values, defaults)
            if code is not None:
                fail(name, code, message or f"参数 {name} 不可用")
                continue
            sources[name] = "default" if not (name in values and self._is_provided(values.get(name))) else "provided"

            if ptype == "list":
                items = self._normalize_list(raw)
                if len(items) == 0:
                    # 显式空集合（或默认值本身为空）与必填 IN 条件冲突，禁止扩大为全表查询
                    fail(
                        name,
                        BINDING_EMPTY,
                        f"参数字段 {name} 收到空集合，必填 IN 条件不允许为空，请至少选择一个取值",
                    )
                    continue
                converted_items = []
                item_error = False
                for item in items:
                    converted, item_code, item_message = self._check_scalar(name, "str", item)
                    if item_code is not None:
                        fail(name, item_code, item_message)
                        item_error = True
                        break
                    converted_items.append(converted)
                if item_error:
                    continue
                resolved[name] = converted_items
            else:
                converted, code, message = self._check_scalar(name, ptype, raw)
                if code is not None:
                    fail(name, code, message)
                    continue
                resolved[name] = converted

        if field_errors:
            # 汇总状态优先级：缺参 > 空集合 > 类型错误
            if BINDING_MISSING in statuses:
                final_status = BINDING_MISSING
            elif BINDING_EMPTY in statuses:
                final_status = BINDING_EMPTY
            else:
                final_status = BINDING_TYPE
            return {
                "ok": False,
                "executable": False,
                "status": final_status,
                "sql": None,
                "params": [],
                "bindings": bindings,
                "field_errors": field_errors,
                "missing_fields": missing_fields,
                "empty_fields": empty_fields,
                "errors": list(field_errors.values()),
            }

        # 校验全部通过后，严格按占位符在 SQL 中的出现顺序展开，
        # 保证每个列表项与 $n 占位符一一对应（日期在前、多城市在后也不会错位）。
        bound_sql = sql_text

        def replace_placeholder(m: re.Match) -> str:
            name = m.group(1)
            ptype = m.group(2) or "str"
            value = resolved[name]
            start_index = len(bound_params) + 1
            if ptype == "list":
                bound_params.extend(value)
                placeholders = ", ".join(f"${start_index + j}" for j in range(len(value)))
                bindings.append({
                    "field": name,
                    "type": "list",
                    "source": sources[name],
                    "value": list(value),
                    "item_count": len(value),
                    "placeholders": [f"${start_index + j}" for j in range(len(value))],
                    "placeholder": placeholders,
                })
                # IN 列表必须带括号，避免 "IN $3, $4" 这类非法 SQL
                return f"({placeholders})"

            bound_params.append(value)
            placeholder = f"${start_index}"
            bindings.append({
                "field": name,
                "type": ptype,
                "source": sources[name],
                "value": value,
                "item_count": 1,
                "placeholders": [placeholder],
                "placeholder": placeholder,
            })
            return placeholder

        bound_sql = self.PLACEHOLDER_RE.sub(replace_placeholder, bound_sql)

        return {
            "ok": True,
            "executable": True,
            "status": BINDING_OK,
            "sql": bound_sql,
            "params": bound_params,
            "bindings": bindings,
            "field_errors": {},
            "missing_fields": [],
            "empty_fields": [],
            "errors": [],
        }

    def bind(self, sql_text: str, values: dict[str, Any]) -> dict[str, Any]:
        """执行参数绑定，返回绑定后的SQL和参数列表（兼容旧调用方）。"""
        result = self.build_binding(sql_text, values)
        return {
            "ok": result["ok"],
            "errors": result["errors"],
            "sql": result["sql"],
            "params": result["params"],
            "status": result["status"],
            "executable": result["executable"],
            "bindings": result["bindings"],
            "field_errors": result["field_errors"],
            "missing_fields": result["missing_fields"],
            "empty_fields": result["empty_fields"],
        }

    def bind_template(self, template_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按模板主键加载模板并生成绑定结果，模板缺失时显式报错。"""
        tmpl = self.get_template(template_id)
        if not tmpl:
            return {
                "ok": False,
                "executable": False,
                "status": BINDING_TEMPLATE,
                "sql": None,
                "params": [],
                "bindings": [],
                "field_errors": {},
                "missing_fields": [],
                "empty_fields": [],
                "errors": [f"模板 {template_id} 不存在"],
            }
        result = self.build_binding(tmpl.get("SQL片段", ""), values, template=tmpl)
        result["template_id"] = template_id
        result["template_code"] = tmpl.get("模板编号", "")
        return result

    def expand_template(self, template_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按模板编号加载并展开（保留旧接口名）。"""
        return self.bind_template(template_id, values)

    def expand_api_query(self, api_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按API接口编号加载SQL并展开（API服务→SQL模板→查询执行链）。"""
        api = store.find("api_service", api_id)
        if not api:
            return {
                "ok": False,
                "executable": False,
                "status": BINDING_TEMPLATE,
                "sql": None,
                "params": [],
                "bindings": [],
                "field_errors": {},
                "missing_fields": [],
                "empty_fields": [],
                "errors": [f"接口 {api_id} 不存在"],
            }
        sql_text = api.get("查询SQL", "")
        tmpl = None
        # 如果接口引用了模板，先加载模板；默认值只允许来自该模板自身
        tmpl_ref = api.get("参数列表", "")
        if tmpl_ref.startswith("SQL-"):
            tmpl = self.find_template_by_code(tmpl_ref)
            if tmpl:
                sql_text = tmpl.get("SQL片段", sql_text)
        result = self.build_binding(sql_text, values, template=tmpl)
        result["api_id"] = api_id
        return result

    def suggest_optimization(self, sql_text: str) -> list[dict[str, str]]:
        """基于SQL文本给出优化建议（与查询优化模块联动）。"""
        suggestions = []
        sql_upper = sql_text.upper()

        if "SELECT *" in sql_upper:
            suggestions.append({
                "type": "column_prune",
                "severity": "warning",
                "message": "避免使用 SELECT *，应显式列出所需字段以减少IO",
            })

        if re.search(r"\bLIKE\s+['\"]%[^'\"]+%['\"]", sql_text, re.IGNORECASE):
            suggestions.append({
                "type": "index_unfriendly",
                "severity": "warning",
                "message": "前后通配LIKE会导致索引失效，建议考虑全文检索或前缀索引",
            })

        if sql_upper.count("JOIN") > 3:
            suggestions.append({
                "type": "join_complexity",
                "severity": "info",
                "message": f"检测到 {sql_upper.count('JOIN')} 个JOIN，建议检查是否可拆分为子查询或物化视图",
            })

        if "ORDER BY" in sql_upper and "LIMIT" not in sql_upper:
            suggestions.append({
                "type": "sort_cost",
                "severity": "info",
                "message": "无LIMIT的ORDER BY可能导致全表排序，建议添加分页限制",
            })

        if re.search(r"\bWHERE\b", sql_upper) and not re.search(r"\bINDEX\b", sql_upper):
            suggestions.append({
                "type": "index_suggest",
                "severity": "info",
                "message": "WHERE条件字段建议创建索引以加速查询",
            })

        return suggestions
