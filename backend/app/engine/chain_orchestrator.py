"""跨模块数据链编排器：管理10组真实数据链的端到端执行。

支持的链路：
1. 指标计算链: 数据源连接→指标域→派生指标→数据聚合
2. 查询服务链: API服务→SQL模板→查询优化→数据预览
3. 消费治理链: 接口订阅→调用凭证→调用追踪→访问看板
4. 数据输出链: 批量导出→数据清洗→结果缓存→查询审计
5. 口径统一链: 指标重名解析→口径注册
6. 版本管理链: 接口版本→灰度发布
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.engine.metric_calculator import MetricCalculator
from app.engine.sql_binder import SqlTemplateBinder
from app.engine.query_optimizer import QueryOptimizer
from app.engine.export_sharder import ExportSharder


class ChainOrchestrator:
    """跨模块数据链编排器。"""

    CHAIN_TYPES = [
        "metric_compute",
        "query_service",
        "consume_governance",
        "data_output",
        "caliber_unify",
        "version_manage",
        "api_lifecycle",
        "cache_warmup",
        "audit_trail",
        "template_reuse",
    ]

    def __init__(self) -> None:
        self.calculator = MetricCalculator()
        self.binder = SqlTemplateBinder()
        self.optimizer = QueryOptimizer()
        self.sharder = ExportSharder()

    # ───────────────────────────────────────────────
    # 链路 1: 指标计算链
    # ───────────────────────────────────────────────
    def run_metric_compute_chain(self, domain_code: str, metric_code: str, aggregate_id: int) -> dict[str, Any]:
        """指标计算链：数据源连接→指标域→派生指标→数据聚合。"""
        result = {"chain": "metric_compute", "steps": [], "ok": True}

        # Step 1: 校验数据源连接
        domain = next((r for r in store.rows("metric_domain") if r.get("域编号") == domain_code), None)
        if not domain:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "datasource_connect", "status": "error", "detail": f"指标域 {domain_code} 不存在"})
            return result

        ds_table = domain.get("数据源表", "")
        ds = next((r for r in store.rows("datasource_connect") if r.get("数据库名") == ds_table or r.get("连接编号") == ds_table), None)
        if not ds or ds.get("status") != "已连接":
            result["steps"].append({"step": 1, "module": "datasource_connect", "status": "warning", "detail": "数据源未连接或不存在"})
        else:
            result["steps"].append({"step": 1, "module": "datasource_connect", "status": "ok", "detail": f"数据源 {ds.get('连接编号')} 已连接"})

        # Step 2: 校验指标域
        if domain.get("status") != "已启用":
            result["ok"] = False
            result["steps"].append({"step": 2, "module": "metric_domain", "status": "error", "detail": f"指标域状态为 {domain.get('status')}"})
            return result
        result["steps"].append({"step": 2, "module": "metric_domain", "status": "ok", "detail": f"指标域 {domain_code} 已启用"})

        # Step 3: 校验并计算派生指标
        validation = self.calculator.validate_metric_chain(domain_code, metric_code)
        if not validation["ok"]:
            result["ok"] = False
            result["steps"].append({"step": 3, "module": "derived_metric", "status": "error", "detail": "; ".join(validation["errors"])})
            return result
        metric = self.calculator.get_metric_by_code(metric_code)
        formula = self.calculator.parse_formula(metric.get("计算公式", ""))
        result["steps"].append({"step": 3, "module": "derived_metric", "status": "ok", "detail": f"派生指标 {metric_code} 公式: {formula.raw}"})

        # Step 4: 数据聚合
        agg = store.find("data_aggregate", aggregate_id)
        if not agg:
            result["ok"] = False
            result["steps"].append({"step": 4, "module": "data_aggregate", "status": "error", "detail": f"聚合配置 {aggregate_id} 不存在"})
            return result
        if agg.get("status") != "已生效":
            result["steps"].append({"step": 4, "module": "data_aggregate", "status": "warning", "detail": f"聚合配置状态为 {agg.get('status')}"})
        else:
            plan = self.calculator.build_compute_plan(aggregate_id)
            result["steps"].append({"step": 4, "module": "data_aggregate", "status": "ok", "detail": f"聚合计划生成成功，共 {len(plan.get('steps', []))} 步"})
            result["compute_plan"] = plan

        return result

    # ───────────────────────────────────────────────
    # 链路 2: 查询服务链
    # ───────────────────────────────────────────────
    def run_query_service_chain(self, api_id: int, template_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """查询服务链：API服务→SQL模板→查询优化→数据预览。"""
        result = {"chain": "query_service", "steps": [], "ok": True}

        # Step 1: API服务
        api = store.find("api_service", api_id)
        if not api:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_service", "status": "error", "detail": f"接口 {api_id} 不存在"})
            return result
        if api.get("status") != "已发布":
            result["steps"].append({"step": 1, "module": "api_service", "status": "warning", "detail": f"接口状态为 {api.get('status')}"})
        else:
            result["steps"].append({"step": 1, "module": "api_service", "status": "ok", "detail": f"接口 {api.get('接口编号')} 已发布"})

        # Step 2: SQL模板绑定（绑定结果是后续优化与预览取样的唯一可执行来源）
        bind_result = self.binder.expand_template(template_id, params)
        if not bind_result.get("ok"):
            result["ok"] = False
            result["steps"].append({
                "step": 2, "module": "sql_template", "status": "error",
                "detail": "; ".join(bind_result.get("errors", [])),
                "binding_status": bind_result.get("status"),
                "field_errors": bind_result.get("field_errors", {}),
            })
            # 绑定不可执行：直接终止，不进入查询优化，更不允许数据预览取样
            result["binding"] = bind_result
            return result
        result["steps"].append({"step": 2, "module": "sql_template", "status": "ok", "detail": "SQL绑定成功", "bound_sql": bind_result["sql"]})

        # Step 3: 查询优化（仅在绑定可执行后才允许介入）
        opt_report = self.optimizer.analyze_sql(bind_result["sql"], api_id=api_id)
        result["steps"].append({"step": 3, "module": "query_optimize", "status": "ok", "detail": f"预估成本 {opt_report.get('estimated_cost', 0)}", "suggestions": opt_report.get("index_suggestions", [])})

        # Step 4: 数据预览——样本来源关联同一份模板参数绑定结果
        preview = next((r for r in store.rows("data_preview") if r.get("模板编号") == api.get("接口编号")), None)
        if preview:
            result["steps"].append({
                "step": 4, "module": "data_preview", "status": "ok",
                "detail": f"预览模板 {preview.get('模板编号')} 已关联绑定结果",
                "preview_id": preview.get("id"),
                "binding_ref": {
                    "status": bind_result.get("status"),
                    "bindings": bind_result.get("bindings", []),
                    "bound_sql": bind_result["sql"],
                },
            })
        else:
            result["steps"].append({"step": 4, "module": "data_preview", "status": "warning", "detail": "未找到关联的预览模板"})

        result["bound_sql"] = bind_result["sql"]
        result["binding"] = bind_result
        result["optimization"] = opt_report
        return result

    # ───────────────────────────────────────────────
    # 链路 3: 消费治理链
    # ───────────────────────────────────────────────
    def run_consume_governance_chain(self, subscribe_id: int) -> dict[str, Any]:
        """消费治理链：接口订阅→调用凭证→调用追踪→访问看板。"""
        result = {"chain": "consume_governance", "steps": [], "ok": True}

        # Step 1: 接口订阅
        sub = store.find("api_subscribe", subscribe_id)
        if not sub:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_subscribe", "status": "error", "detail": f"订阅 {subscribe_id} 不存在"})
            return result
        if sub.get("status") != "已生效":
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_subscribe", "status": "error", "detail": f"订阅状态为 {sub.get('status')}"})
            return result
        result["steps"].append({"step": 1, "module": "api_subscribe", "status": "ok", "detail": f"订阅 {sub.get('订阅编号')} 已生效"})

        # Step 2: 调用凭证
        cred = next((r for r in store.rows("call_credential") if r.get("订阅方") == sub.get("订阅方")), None)
        if not cred or cred.get("status") not in ("已签发", "使用中"):
            result["ok"] = False
            result["steps"].append({"step": 2, "module": "call_credential", "status": "error", "detail": "凭证未签发或已过期"})
            return result
        result["steps"].append({"step": 2, "module": "call_credential", "status": "ok", "detail": f"凭证 {cred.get('凭证编号')} 有效"})

        # Step 3: 调用追踪
        traces = [r for r in store.rows("call_trace") if r.get("接口编号") == sub.get("接口编号")]
        result["steps"].append({"step": 3, "module": "call_trace", "status": "ok", "detail": f"找到 {len(traces)} 条调用记录"})

        # Step 4: 访问看板
        dashboard = next((r for r in store.rows("access_dashboard") if r.get("接口编号") == sub.get("接口编号")), None)
        if dashboard:
            result["steps"].append({"step": 4, "module": "access_dashboard", "status": "ok", "detail": f"看板统计已生成，今日调用量 {dashboard.get('今日调用量', 0)}"})
        else:
            result["steps"].append({"step": 4, "module": "access_dashboard", "status": "warning", "detail": "看板统计未生成"})

        return result

    # ───────────────────────────────────────────────
    # 链路 4: 数据输出链
    # ───────────────────────────────────────────────
    def run_data_output_chain(self, export_id: int) -> dict[str, Any]:
        """数据输出链：批量导出→数据清洗→结果缓存→查询审计。"""
        result = {"chain": "data_output", "steps": [], "ok": True}

        # Step 1: 批量导出
        task = store.find("batch_export", export_id)
        if not task:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "batch_export", "status": "error", "detail": f"导出任务 {export_id} 不存在"})
            return result
        shard_result = self.sharder.generate_shards(export_id)
        result["steps"].append({"step": 1, "module": "batch_export", "status": "ok", "detail": f"分片计划生成，共 {shard_result.get('shard_count', 0)} 片"})

        # Step 2: 数据清洗
        cleanse = next((r for r in store.rows("data_cleanse") if r.get("数据来源") == task.get("任务编号")), None)
        if cleanse and cleanse.get("status") == "已生效":
            result["steps"].append({"step": 2, "module": "data_cleanse", "status": "ok", "detail": f"清洗规则 {cleanse.get('清洗规则')} 已生效"})
        else:
            result["steps"].append({"step": 2, "module": "data_cleanse", "status": "warning", "detail": "清洗配置未生效"})

        # Step 3: 结果缓存
        cache = next((r for r in store.rows("result_cache") if task.get("任务编号") in str(r.get("缓存键", ""))), None)
        if cache and cache.get("status") == "已生效":
            result["steps"].append({"step": 3, "module": "result_cache", "status": "ok", "detail": f"缓存配置 {cache.get('配置编号')} 已生效"})
        else:
            result["steps"].append({"step": 3, "module": "result_cache", "status": "warning", "detail": "缓存配置未生效"})

        # Step 4: 查询审计
        audit = next((r for r in store.rows("query_audit") if task.get("任务编号") in str(r.get("请求参数", ""))), None)
        if audit:
            result["steps"].append({"step": 4, "module": "query_audit", "status": "ok", "detail": f"审计记录 {audit.get('日志编号')} 已存在"})
        else:
            result["steps"].append({"step": 4, "module": "query_audit", "status": "warning", "detail": "审计记录缺失"})

        result["shard_plan"] = shard_result
        return result

    # ───────────────────────────────────────────────
    # 链路 5: 口径统一链
    # ───────────────────────────────────────────────
    def run_caliber_unify_chain(self, homonym_id: int) -> dict[str, Any]:
        """口径统一链：指标重名解析→口径注册。"""
        result = {"chain": "caliber_unify", "steps": [], "ok": True}

        hom = store.find("metric_homonym", homonym_id)
        if not hom:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "metric_homonym", "status": "error", "detail": f"重名记录 {homonym_id} 不存在"})
            return result

        if hom.get("status") != "已统一":
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "metric_homonym", "status": "error", "detail": f"裁决状态为 {hom.get('status')}"})
            return result

        result["steps"].append({"step": 1, "module": "metric_homonym", "status": "ok", "detail": f"指标 {hom.get('指标名称')} 已统一口径"})

        # 检查是否已注册到指标域
        domain_code = hom.get("生效域", "")
        domain = next((r for r in store.rows("metric_domain") if r.get("域编号") == domain_code), None)
        if domain:
            result["steps"].append({"step": 2, "module": "metric_domain", "status": "ok", "detail": f"口径已注册到域 {domain_code}"})
        else:
            result["steps"].append({"step": 2, "module": "metric_domain", "status": "warning", "detail": f"生效域 {domain_code} 未找到"})

        # 检查派生指标是否更新
        derived = next((r for r in store.rows("derived_metric") if r.get("指标编号") == hom.get("指标名称")), None)
        if derived:
            result["steps"].append({"step": 3, "module": "derived_metric", "status": "ok", "detail": f"派生指标 {derived.get('指标编号')} 已关联统一口径"})
        else:
            result["steps"].append({"step": 3, "module": "derived_metric", "status": "warning", "detail": "未找到关联派生指标"})

        return result

    # ───────────────────────────────────────────────
    # 链路 6: 版本管理链
    # ───────────────────────────────────────────────
    def run_version_manage_chain(self, version_id: int) -> dict[str, Any]:
        """版本管理链：接口版本→灰度发布。"""
        result = {"chain": "version_manage", "steps": [], "ok": True}

        ver = store.find("api_version", version_id)
        if not ver:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_version", "status": "error", "detail": f"版本 {version_id} 不存在"})
            return result

        api_id_str = ver.get("接口编号", "")
        api = next((r for r in store.rows("api_service") if r.get("接口编号") == api_id_str), None)
        if not api:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_service", "status": "error", "detail": f"关联接口 {api_id_str} 不存在"})
            return result

        status = ver.get("status", "")
        if status == "灰度中":
            result["steps"].append({"step": 1, "module": "api_version", "status": "ok", "detail": f"版本 {ver.get('版本编号')} 灰度发布中"})
            result["steps"].append({"step": 2, "module": "api_service", "status": "ok", "detail": f"接口 {api_id_str} 处于灰度状态"})
        elif status == "已全量":
            result["steps"].append({"step": 1, "module": "api_version", "status": "ok", "detail": f"版本 {ver.get('版本编号')} 已全量发布"})
            result["steps"].append({"step": 2, "module": "api_service", "status": "ok", "detail": f"接口 {api_id_str} 已全量切换"})
        elif status == "已回滚":
            result["steps"].append({"step": 1, "module": "api_version", "status": "ok", "detail": f"版本 {ver.get('版本编号')} 已回滚"})
            rollback_ver = ver.get("回滚版本", "")
            result["steps"].append({"step": 2, "module": "api_service", "status": "ok", "detail": f"接口 {api_id_str} 已回滚至 {rollback_ver}"})
        else:
            result["steps"].append({"step": 1, "module": "api_version", "status": "warning", "detail": f"版本状态为 {status}，未发布"})

        return result

    # ───────────────────────────────────────────────
    # 链路 7: API生命周期链（订阅→凭证→追踪→审计→看板）
    # ───────────────────────────────────────────────
    def run_api_lifecycle_chain(self, api_code: str) -> dict[str, Any]:
        """API生命周期链：完整追踪一个接口从发布到消费的全链路。"""
        result = {"chain": "api_lifecycle", "steps": [], "ok": True}

        api = next((r for r in store.rows("api_service") if r.get("接口编号") == api_code), None)
        if not api:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_service", "status": "error", "detail": f"接口 {api_code} 不存在"})
            return result
        result["steps"].append({"step": 1, "module": "api_service", "status": "ok", "detail": f"接口 {api_code} 状态 {api.get('status')}"})

        subs = [r for r in store.rows("api_subscribe") if r.get("接口编号") == api_code]
        result["steps"].append({"step": 2, "module": "api_subscribe", "status": "ok", "detail": f"订阅数 {len(subs)}"})

        creds = [r for r in store.rows("call_credential") if any(r.get("订阅方") == s.get("订阅方") for s in subs)]
        result["steps"].append({"step": 3, "module": "call_credential", "status": "ok", "detail": f"凭证数 {len(creds)}"})

        traces = [r for r in store.rows("call_trace") if r.get("接口编号") == api_code]
        result["steps"].append({"step": 4, "module": "call_trace", "status": "ok", "detail": f"调用记录 {len(traces)}"})

        audits = [r for r in store.rows("query_audit") if r.get("接口编号") == api_code]
        result["steps"].append({"step": 5, "module": "query_audit", "status": "ok", "detail": f"审计记录 {len(audits)}"})

        dash = next((r for r in store.rows("access_dashboard") if r.get("接口编号") == api_code), None)
        if dash:
            result["steps"].append({"step": 6, "module": "access_dashboard", "status": "ok", "detail": f"看板已生成"})
        else:
            result["steps"].append({"step": 6, "module": "access_dashboard", "status": "warning", "detail": "看板未生成"})

        return result

    # ───────────────────────────────────────────────
    # 链路 8: 缓存预热链（API→模板→缓存配置→数据预览）
    # ───────────────────────────────────────────────
    def run_cache_warmup_chain(self, api_id: int) -> dict[str, Any]:
        """缓存预热链：为高频API预热结果缓存。"""
        result = {"chain": "cache_warmup", "steps": [], "ok": True}

        api = store.find("api_service", api_id)
        if not api:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "api_service", "status": "error", "detail": f"接口 {api_id} 不存在"})
            return result
        result["steps"].append({"step": 1, "module": "api_service", "status": "ok", "detail": f"接口 {api.get('接口编号')} 缓存时长 {api.get('缓存时长')}s"})

        tmpl = next((r for r in store.rows("sql_template") if r.get("模板编号") == api.get("参数列表")), None)
        if tmpl:
            result["steps"].append({"step": 2, "module": "sql_template", "status": "ok", "detail": f"模板 {tmpl.get('模板编号')} 已关联"})
        else:
            result["steps"].append({"step": 2, "module": "sql_template", "status": "warning", "detail": "模板未关联"})

        cache = next((r for r in store.rows("result_cache") if api.get("接口编号") in str(r.get("缓存键", ""))), None)
        if cache:
            result["steps"].append({"step": 3, "module": "result_cache", "status": "ok", "detail": f"缓存配置 {cache.get('配置编号')} 已关联"})
        else:
            result["steps"].append({"step": 3, "module": "result_cache", "status": "warning", "detail": "缓存配置未关联"})

        preview = next((r for r in store.rows("data_preview") if r.get("模板编号") == api.get("接口编号")), None)
        if preview:
            result["steps"].append({"step": 4, "module": "data_preview", "status": "ok", "detail": f"预览模板 {preview.get('模板编号')} 已关联"})
        else:
            result["steps"].append({"step": 4, "module": "data_preview", "status": "warning", "detail": "预览模板未关联"})

        return result

    # ───────────────────────────────────────────────
    # 链路 9: 审计追踪链（调用→追踪→审计→合规结论）
    # ───────────────────────────────────────────────
    def run_audit_trail_chain(self, trace_id: int) -> dict[str, Any]:
        """审计追踪链：从一次调用记录生成完整审计结论。"""
        result = {"chain": "audit_trail", "steps": [], "ok": True}

        trace = store.find("call_trace", trace_id)
        if not trace:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "call_trace", "status": "error", "detail": f"调用记录 {trace_id} 不存在"})
            return result
        result["steps"].append({"step": 1, "module": "call_trace", "status": "ok", "detail": f"调用记录 {trace.get('记录编号')} 耗时 {trace.get('耗时毫秒')}ms"})

        # 检查是否已存在审计记录
        audit = next((r for r in store.rows("query_audit") if r.get("接口编号") == trace.get("接口编号") and r.get("调用方") == trace.get("调用方")), None)
        if audit:
            result["steps"].append({"step": 2, "module": "query_audit", "status": "ok", "detail": f"审计记录 {audit.get('日志编号')} 已存在，结论: {audit.get('审计结论')}"})
            result["audit_conclusion"] = audit.get("审计结论", "")
        else:
            # 自动生成审计结论
            sensitivity = "高" if int(trace.get("返回行数", 0) or 0) > 10000 else "中" if int(trace.get("返回行数", 0) or 0) > 1000 else "低"
            conclusion = "正常" if trace.get("status") == "正常" else "待审核" if trace.get("status") == "慢查询" else "异常"
            result["steps"].append({"step": 2, "module": "query_audit", "status": "ok", "detail": f"自动生成审计结论: {conclusion}，敏感度: {sensitivity}"})
            result["audit_conclusion"] = conclusion
            result["sensitivity"] = sensitivity

        return result

    # ───────────────────────────────────────────────
    # 链路 10: 模板复用链（模板→API→优化→版本）
    # ───────────────────────────────────────────────
    def run_template_reuse_chain(self, template_id: int) -> dict[str, Any]:
        """模板复用链：追踪SQL模板在多个API中的复用情况。"""
        result = {"chain": "template_reuse", "steps": [], "ok": True}

        tmpl = store.find("sql_template", template_id)
        if not tmpl:
            result["ok"] = False
            result["steps"].append({"step": 1, "module": "sql_template", "status": "error", "detail": f"模板 {template_id} 不存在"})
            return result
        result["steps"].append({"step": 1, "module": "sql_template", "status": "ok", "detail": f"模板 {tmpl.get('模板编号')} 已使用 {tmpl.get('使用次数', 0)} 次"})

        apis = [r for r in store.rows("api_service") if tmpl.get("模板编号") in str(r.get("参数列表", ""))]
        result["steps"].append({"step": 2, "module": "api_service", "status": "ok", "detail": f"关联API数 {len(apis)}: {', '.join(a.get('接口编号', '') for a in apis[:3])}"})

        for api in apis[:3]:
            opt = self.optimizer.analyze_sql(api.get("查询SQL", ""), api_id=api.get("id"))
            if opt.get("estimated_cost", 0) > 2000:
                result["steps"].append({"step": 3, "module": "query_optimize", "status": "warning", "detail": f"API {api.get('接口编号')} 成本高 {opt.get('estimated_cost')}"})
            else:
                result["steps"].append({"step": 3, "module": "query_optimize", "status": "ok", "detail": f"API {api.get('接口编号')} 成本 {opt.get('estimated_cost')}"})

        versions = [r for r in store.rows("api_version") if r.get("接口编号") in [a.get("接口编号", "") for a in apis]]
        result["steps"].append({"step": 4, "module": "api_version", "status": "ok", "detail": f"关联版本数 {len(versions)}"})

        return result

