"""麦当劳 MCP 业务适配层。

把 35 个工具中本项目真正用到的只读能力，封装成语义清晰的业务方法。
推荐算法完全不感知 MCP 协议细节，便于测试与替换数据源。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..models import (
    Candidate,
    DataOrigin,
    Money,
    Nutrition,
    PriceQuote,
    QueryStatus,
    Store,
    StoreContext,
    match_nutrition,
    parse_nutrition_index,
)

logger = logging.getLogger(__name__)


def _clean_text(value: Any) -> str | None:
    """清洗 MCP 返回的文本字段。

    实测发现麦当劳 MCP 返回的品类名里可能含换行与分隔符，
    例如 "鸡肉汉堡\n/卷"、"小食拼盘\n/多人餐"。这类内容如果不处理，
    下游的关键词匹配会因为中间插入换行而失效。
    """
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\n", "").replace("\r", "").strip()
    return cleaned or None


class McDService:
    """只读业务门面。

    刻意不实现任何下单、领券、抽奖、取消订单的方法。
    """

    def __init__(self, client: Any, max_workers: int = 4) -> None:
        self._client = client
        self._max_workers = max_workers
        self._nutrition_index: dict[str, Nutrition] | None = None

    # ------------------------------------------------------------------
    # 门店
    # ------------------------------------------------------------------

    def find_stores(
        self, city: str, keyword: str, be_type: int = 1, search_type: int = 2
    ) -> list[Store]:
        """查询可点餐门店。已实测：searchType=2 时 city 与 keyword 必填。"""
        payload, _ = self._client.call(
            "query-nearby-stores",
            {
                "searchType": search_type,
                "city": city,
                "keyword": keyword,
                "beType": be_type,
            },
        )
        data = payload.get("data") or []
        if not isinstance(data, list):
            return []
        return [Store.from_payload(item) for item in data if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------

    def list_menu(self, context: StoreContext) -> list[Candidate]:
        """拉取门店菜单。

        重要：实测确认该接口只返回 productCode 与 tags，
        不含名称与价格。此处产出的 Candidate 其 price 为 None、
        name 为 None，必须经过 enrich_by_price 与 enrich_by_name 才有意义。
        """
        arguments: dict[str, Any] = {
            "storeCode": context.store.store_code,
            "beType": context.be_type,
            "orderType": context.order_type,
        }
        if context.needs_be_code and context.store.be_code:
            arguments["beCode"] = context.store.be_code
        payload, _ = self._client.call("query-meals", arguments)
        data = payload.get("data") or {}
        categories = data.get("categories") or []

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for category in categories:
            if not isinstance(category, dict):
                continue
            category_name = _clean_text(category.get("name"))
            for meal in category.get("meals") or []:
                if not isinstance(meal, dict):
                    continue
                code = str(meal.get("code") or "")
                if not code or code in seen:
                    continue
                seen.add(code)
                candidates.append(
                    Candidate(
                        product_code=code,
                        category=category_name,
                        tags=[
                            cleaned
                            for cleaned in (_clean_text(tag) for tag in (meal.get("tags") or []))
                            if cleaned
                        ],
                    )
                )
        return candidates

    def fetch_detail(self, context: StoreContext, code: str) -> dict[str, Any] | None:
        """查询单个餐品详情，提供真实名称与套餐结构。"""
        try:
            payload, _ = self._client.call(
                "query-meal-detail",
                {
                    "storeCode": context.store.store_code,
                    "beType": context.be_type,
                    "orderType": context.order_type,
                    "code": code,
                    **(
                        {"beCode": context.store.be_code}
                        if context.needs_be_code and context.store.be_code
                        else {}
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 详情缺失不应中断整体流程
            logger.info("query-meal-detail(%s) 失败：%s", code, exc)
            return None

        data = payload.get("data")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 价格（本项目唯一的真实价格来源）
    # ------------------------------------------------------------------

    def calculate_price(
        self, context: StoreContext, items: list[dict[str, Any]]
    ) -> PriceQuote | None:
        """计算真实价格。已实测：金额以分返回，items 字段为 productCode/quantity。"""
        if not items:
            return None
        try:
            payload, _ = self._client.call(
                "calculate-price", context.price_arguments(items)
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("calculate-price 失败：%s", exc)
            return None
        if not payload or not isinstance(payload.get("data"), dict):
            return None
        return PriceQuote.from_payload(payload)

    def quote_candidates(
        self, context: StoreContext, candidates: list[Candidate], batch_size: int = 12
    ) -> list[Candidate]:
        """批量为候选填充真实价格。

        MCP 的 calculate-price 接受多个 items，因此按批组合而非逐个调用，
        把 N 次网络往返压缩到约 N/batch_size 次。
        只有成功返回的批次才会写入价格，其余保持 None（禁止编造）。
        """
        if not candidates:
            return []

        by_code = {candidate.product_code: candidate for candidate in candidates}
        codes = [code for code in by_code if code]
        confirmed: dict[str, int] = {}

        batches = [codes[i : i + batch_size] for i in range(0, len(codes), batch_size)]
        try:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                results = list(
                    pool.map(
                        lambda batch: self.calculate_price(
                            context,
                            [{"productCode": code, "quantity": 1} for code in batch],
                        ),
                        batches,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 线程池异常不应中断整体流程
            logger.error("批量算价线程池异常：%s", exc)
            results = [None] * len(batches)

        for batch, quote in zip(batches, results):
            if quote is None or not quote.items:
                continue
            for item in quote.items:
                code = str(item.get("productCode") or "")
                subtotal = item.get("subtotal")
                if code and isinstance(subtotal, (int, float)):
                    confirmed[code] = int(subtotal)

        for code, candidate in by_code.items():
            if code in confirmed:
                candidate.price = Money(confirmed[code])
                candidate.price_status = QueryStatus.OK
                candidate.origin = DataOrigin.REAL_MCP
            else:
                candidate.price_status = QueryStatus.FAILED
        return candidates

    def enrich_by_name(self, context: StoreContext, candidates: list[Candidate]) -> list[Candidate]:
        """通过 query-meal-detail 补齐真实名称、套餐子项与营养匹配。"""
        nutrition_index = self.nutrition_index()
        for candidate in candidates:
            detail = self.fetch_detail(context, candidate.product_code)
            if not detail:
                candidate.name_status = QueryStatus.FAILED
                continue
            name = _clean_text(detail.get("name"))
            if name:
                candidate.name = name
                candidate.name_status = QueryStatus.OK
                candidate.origin = DataOrigin.REAL_MCP
                candidate.nutrition = match_nutrition(name, nutrition_index)

            combo_items: list[str] = []
            for round_item in detail.get("rounds") or []:
                if not isinstance(round_item, dict):
                    continue
                for choice in round_item.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    chosen_name = _clean_text(choice.get("name"))
                    if chosen_name and choice.get("quantity", 0) >= 1:
                        combo_items.append(chosen_name)
            if combo_items:
                candidate.combo_items = combo_items
        return candidates

    # ------------------------------------------------------------------
    # 优惠与营养
    # ------------------------------------------------------------------

    def nutrition_index(self) -> dict[str, Nutrition]:
        """营养数据索引，全流程只拉取一次。"""
        if self._nutrition_index is None:
            try:
                payload, _ = self._client.call("list-nutrition-foods", {})
                self._nutrition_index = parse_nutrition_index(payload)
            except Exception as exc:  # noqa: BLE001
                logger.info("list-nutrition-foods 失败：%s", exc)
                self._nutrition_index = {}
        return self._nutrition_index

    def available_coupons(self) -> list[str]:
        """可领取的券标题列表。

        该工具实测返回 Markdown 文本而非 JSON，因此做保守的文本提取。
        拿不到就返回空列表 —— 绝不据此承诺"一定有券可用"。
        """
        try:
            payload, _ = self._client.call("available-coupons", {})
        except Exception as exc:  # noqa: BLE001
            logger.info("available-coupons 失败：%s", exc)
            return []

        if isinstance(payload, dict):
            # 兜底：某些情况下也可能以结构化形式返回
            data = payload.get("data")
            titles: list[str] = []
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("couponName")
                    if isinstance(title, str) and title:
                        titles.append(title)
            if titles:
                return list(dict.fromkeys(titles))
            payload = payload.get("message") or ""

        if not isinstance(payload, str):
            return []

        titles = []
        for line in payload.splitlines():
            line = line.strip()
            if line.startswith("- 优惠券标题："):
                title = line[len("- 优惠券标题：") :].strip().rstrip("\\").strip()
                if title:
                    titles.append(title)
        return list(dict.fromkeys(titles))

    def store_coupons(self, context: StoreContext) -> list[dict[str, Any]]:
        """该门店当前可用的券。

        实测提醒：券与门店、渠道强绑定，特定门店可能返回空数组，
        这属于正常业务结果，必须如实告知用户"该门店暂无可用券"。
        """
        try:
            payload, _ = self._client.call(
                "query-store-coupons",
                {
                    "storeCode": context.store.store_code,
                    "beType": context.be_type,
                    "orderType": context.order_type,
                    **(
                        {"beCode": context.store.be_code}
                        if context.needs_be_code and context.store.be_code
                        else {}
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("query-store-coupons 失败：%s", exc)
            return []
        data = payload.get("data")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # 时间
    # ------------------------------------------------------------------

    def current_time(self) -> dict[str, Any] | None:
        """服务器当前时间，避免本地时钟与门店系统不一致。"""
        try:
            payload, _ = self._client.call("now-time-info", {})
        except Exception as exc:  # noqa: BLE001
            logger.info("now-time-info 失败：%s", exc)
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None