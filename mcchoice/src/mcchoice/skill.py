"""McChoice Skill 编排层。

把意图解析、MCP 数据获取、约束过滤、排序、格式化串成一条完整流程。
这是 Agent 实际调用的入口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import formatter
from .adapters.mcd_client import ClientConfig, McDClient, McDError
from .adapters.service import McDService
from .engines.recommender import (
    ScoredCandidate,
    detect_conflict,
    filter_candidates,
    matches_exclusion,
    matches_preference,
    pick_three,
    plan_combo,
    score_candidates,
)
from .intent import DiningIntent, build_followup_questions, parse_intent
from .models import Candidate, Store, StoreContext

logger = logging.getLogger(__name__)


@dataclass
class McChoiceResult:
    """一次决策的完整结果。"""

    answer: str
    intent: DiningIntent
    store: Store | None = None
    candidates_considered: int = 0
    candidates_passed: int = 0
    data_source: str = "real_mcp"
    followup_questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coupon_titles: list[str] = field(default_factory=list)


class McChoiceSkill:
    """麦麦选择困难症终结者。

    严格只读：不下单、不领券、不抽奖、不改地址。
    """

    name = "mcchoice"
    version = "1.0.0"
    description = "基于麦当劳 MCP 的点餐决策助手，用真实菜单与真实算价给出有理由的推荐。"

    def __init__(
        self,
        service: McDService,
        *,
        batch_size: int = 12,
        enable_coupon_lookup: bool = True,
        detail_limit: int = 24,
    ) -> None:
        self._service = service
        self._batch_size = batch_size
        self._enable_coupon_lookup = enable_coupon_lookup
        self._detail_limit = detail_limit

    @classmethod
    def from_env(cls, **kwargs: Any) -> "McChoiceSkill":
        """从环境变量 MCD_MCP_TOKEN 创建真实实例。"""
        import os

        config = ClientConfig(token=os.environ.get("MCD_MCP_TOKEN"))
        return cls(McDService(McDClient(config)), **kwargs)

    # ------------------------------------------------------------------

    def decide(
        self,
        user_text: str,
        *,
        city: str | None = None,
        keyword: str | None = None,
        be_type: int = 1,
    ) -> McChoiceResult:
        """主入口。

        完整流程：解析意图 → 定位门店 → 拉菜单 → 真实算价 → 补名称与营养
        → 硬约束过滤 → 评分排序 → 格式化输出。
        """
        intent = parse_intent(user_text, location=city or keyword)

        # 1. 定位门店
        resolved_city = city or intent.location
        resolved_keyword = keyword or intent.location
        if not resolved_city or not resolved_keyword:
            questions = build_followup_questions(intent) or [
                "告诉我城市和商圈（比如「南京新街口」），我按那附近门店的真实菜单来。"
            ]
            return McChoiceResult(
                answer="### 先确认一下你在哪\n\n" + "\n".join(f"- {q}" for q in questions),
                intent=intent,
                followup_questions=questions,
            )

        try:
            stores = self._service.find_stores(resolved_city, resolved_keyword, be_type)
        except McDError as exc:
            return McChoiceResult(
                answer=formatter.format_mcp_failure(exc.user_facing_note()),
                intent=intent,
                warnings=[exc.kind],
            )

        if not stores:
            return McChoiceResult(
                answer=formatter.format_no_match(
                    f"在「{resolved_city} {resolved_keyword}」附近没有查到可点餐的麦当劳门店。"
                ),
                intent=intent,
            )

        store = stores[0]
        context = StoreContext(store=store, be_type=be_type, order_type=1)

        # 2. 拉菜单（只有 code 和标签，无名称无价格）
        try:
            candidates = self._service.list_menu(context)
        except McDError as exc:
            return McChoiceResult(
                answer=formatter.format_mcp_failure(exc.user_facing_note()),
                intent=intent,
                store=store,
                warnings=[exc.kind],
            )

        if not candidates:
            return McChoiceResult(
                answer=formatter.format_no_match(f"{store.store_name} 当前没有返回可用菜单。"),
                intent=intent,
                store=store,
            )

        # 3. 真实算价（唯一的真实价格来源）
        candidates = self._service.quote_candidates(context, candidates, self._batch_size)

        # 4. 补名称与营养。只对可能进入推荐的候选做详情查询，避免无谓调用。
        shortlist = self._preselect(candidates, intent)
        shortlist = self._service.enrich_by_name(context, shortlist)

        # 5. 硬约束过滤
        filter_result = filter_candidates(shortlist, intent)

        server_time = None
        time_info = self._service.current_time()
        if time_info:
            server_time = time_info.get("formatted")

        # 6. 排序
        scored = score_candidates(filter_result.passed, intent)

        # 优惠券信息（只查，不领）
        coupon_titles: list[str] = []
        store_coupon_count: int | None = None
        if self._enable_coupon_lookup:
            coupon_titles = self._service.available_coupons()
            store_coupons = self._service.store_coupons(context)
            store_coupon_count = len(store_coupons)

        warnings: list[str] = []

        # 7. 无可用候选
        if not scored:
            conflict = detect_conflict(intent, filter_result)
            answer = formatter.format_no_match(conflict or "当前条件下没有可用选项。")
            if coupon_titles:
                answer += "\n\n" + formatter.format_coupons(coupon_titles, store_coupon_count)
            return McChoiceResult(
                answer=answer,
                intent=intent,
                store=store,
                candidates_considered=len(shortlist),
                candidates_passed=0,
                warnings=warnings,
                coupon_titles=coupon_titles,
            )

        # 8. 组合规划（多人或大预算）
        use_combo = intent.party_size > 1 or (
            intent.budget_cents is not None and intent.budget_cents >= 4000
        )
        if use_combo:
            combo_items, combo_total = plan_combo([s.candidate for s in scored], intent)
            if len(combo_items) > 1:
                answer = formatter.format_combo_plan(
                    combo_items, combo_total, store=store, server_time=server_time
                )
            elif combo_items:
                # 只有一件可确认价格的商品：按单选输出，不硬凑组合
                answer = self._format_picks(picks, intent, store, server_time)
            else:
                # 预算放不下任何一件，如实告知条件冲突
                conflict = detect_conflict(intent, filter_result)
                answer = formatter.format_no_match(
                    conflict or "在当前预算下，没有能由 MCP 价格确认的选项。"
                )
        else:
            picks = pick_three(scored)
            answer = self._format_picks(picks, intent, store, server_time)

        if coupon_titles:
            answer += "\n\n" + formatter.format_coupons(coupon_titles, store_coupon_count)

        unpriced = sum(1 for item in scored if not item.candidate.has_confirmed_price())
        if unpriced:
            warnings.append(f"{unpriced} 个候选的价格未通过 MCP 确认，已排除在价格承诺之外")

        return McChoiceResult(
            answer=answer,
            intent=intent,
            store=store,
            candidates_considered=len(shortlist),
            candidates_passed=len(scored),
            warnings=warnings,
            coupon_titles=coupon_titles,
            followup_questions=build_followup_questions(intent),
        )

    # ------------------------------------------------------------------

    def _preselect(self, candidates: list[Candidate], intent: DiningIntent) -> list[Candidate]:
        """在详情查询之前先粗筛，减少无意义的 MCP 调用。

        粗筛只用价格与标签，宽松到不会误杀真正的答案。
        """
        pool = list(candidates)
        if intent.budget_cents is not None:
            affordable = [
                item
                for item in pool
                if item.price is None or item.price.cents <= intent.budget_cents * 1.15
            ]
            if affordable:
                pool = affordable

        if intent.exclusions:
            # 复用过滤阶段的同一个判定，避免两处实现不一致。
            # 曾经的写法 item.category or "" + tags 因运算符优先级问题
            # 会把 tags 整个丢弃，导致排除项过滤失效。
            filtered = [
                item for item in pool if matches_exclusion(item, intent.exclusions) is None
            ]
            if filtered:
                pool = filtered

        # 优先价格明确的（评分需要），其次标签匹配的。
        # 注意括号：匹配文本必须包含品类、标签与名称三部分。
        pool.sort(
            key=lambda item: (
                item.price is None,
                -sum(1 for hint in intent.preferences if matches_preference(item, hint)),
                item.product_code,
            )
        )
        return pool[: self._detail_limit]

    def _format_picks(
        self,
        picks: dict[str, ScoredCandidate | None],
        intent: DiningIntent,
        store: Store | None,
        server_time: str | None,
    ) -> str:
        """按用户意图决定输出一个还是三个方案。"""
        best = picks.get("稳妥")
        if best is None:
            return formatter.format_no_match("当前条件下没有可用选项。")
        if intent.wants_single_answer:
            return formatter.format_single(best, store=store, total=None, server_time=server_time)
        return formatter.format_three(picks, store=store, server_time=server_time)