"""领域模型与候选标准化。

麦当劳 MCP 的原始返回有两个绕不开的坑，本模块负责把它们抹平：

坑一：`query-meals` 只返回 `code` 和 `tags`，既没有名称也没有价格。
      名称必须由 `query-meal-detail` 提供，价格必须由 `calculate-price`
      提供。任何声称"从菜单接口直接读到价格"的实现都是错的。

坑二：金额一律以「分」为单位（2790 表示 ¥27.90）。

标准化后的 Candidate 会显式记录每个字段的来源与可信度，
下游引擎据此决定能否声称"已确认在预算内"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DataOrigin(str, Enum):
    """字段来源标记。用于在输出中向用户诚实标注可信度。"""

    REAL_MCP = "real_mcp"
    """来自真实 MCP 调用，已确认。"""

    UNCONFIRMED = "unconfirmed"
    """字段缺失，无法确认。禁止据此声称价格或规格。"""


class QueryStatus(str, Enum):
    """查询状态。用于区分"查到了""没查到""查失败"。"""

    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Money:
    """金额。内部统一用「分」，只在输出层转换。"""

    cents: int

    @classmethod
    def from_cents(cls, cents: Any) -> "Money | None":
        if cents is None:
            return None
        try:
            value = int(cents)
        except (TypeError, ValueError):
            return None
        return cls(value) if value >= 0 else None

    @property
    def yuan(self) -> float:
        return self.cents / 100.0

    def __add__(self, other: "Money") -> "Money":
        return Money(self.cents + other.cents)

    def __str__(self) -> str:
        return f"{self.yuan:.2f}"


@dataclass
class Store:
    """门店信息，来自 query-nearby-stores。"""

    store_code: str
    store_name: str
    address: str
    distance_m: int | None
    business_start: str | None
    business_end: str | None
    be_code: str | None = None
    """业务编码。得来速/外送场景必传；到店自取为 None。"""

    supports_reservation: bool = False
    reservation_options: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "Store":
        distance = raw.get("distance")
        return cls(
            store_code=str(raw.get("storeCode") or ""),
            store_name=raw.get("storeName") or "未知门店",
            address=raw.get("address") or "",
            distance_m=int(distance) if isinstance(distance, (int, float)) else None,
            business_start=raw.get("businessStartTime"),
            business_end=raw.get("businessEndTime"),
            be_code=raw.get("beCode") or None,
            supports_reservation=bool(raw.get("reservation")),
            reservation_options=raw.get("reservationTimeOptions") or [],
        )


@dataclass
class Nutrition:
    """营养数据，来自 list-nutrition-foods。

    注意：该工具返回 160 条常见餐品数据，但并非与门店菜单一一对应，
    因此匹配失败时必须保持 None，不允许按名称猜测营养值。
    """

    energy_kcal: int | None = None
    protein: int | None = None
    fat: int | None = None
    carbohydrate: int | None = None
    sodium: int | None = None


@dataclass
class Candidate:
    """标准化后的候选商品。

    只允许填入 MCP 真实返回的字段。任何无法从 MCP 确认的值，
    一律保持 None 并由 data_status 标记，不得用默认值或推断填充。
    """

    product_code: str
    name: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)

    price: Money | None = None
    """真实价格，由 calculate-price 确认。若为 None，禁止声称价格。"""

    original_price: Money | None = None
    discount_cents: int | None = None

    nutrition: Nutrition = field(default_factory=Nutrition)
    combo_items: list[str] = field(default_factory=list)
    """套餐子项名称，来自 query-meal-detail。"""

    origin: DataOrigin = DataOrigin.UNCONFIRMED
    name_status: QueryStatus = QueryStatus.SKIPPED
    price_status: QueryStatus = QueryStatus.SKIPPED

    def has_confirmed_price(self) -> bool:
        return self.price is not None and self.price_status is QueryStatus.OK

    def describe_price(self) -> str:
        """面向用户的可确认价格描述。"""
        if self.price is None:
            return "价格暂无法确认"
        text = f"¥{self.price}"
        if self.discount_cents:
            text += f"（已优惠 ¥{self.discount_cents / 100:.2f}）"
        return text


@dataclass
class PriceQuote:
    """来自 calculate-price 的完整报价，用于向用户展示真实账单。"""

    items: list[dict[str, Any]]
    total: Money | None
    original_total: Money | None
    discount: Money | None
    take_ways: list[dict[str, str]]
    server_datetime: str | None
    trace_id: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PriceQuote":
        data = payload.get("data") or {}
        take_ways = [
            {"code": item.get("code"), "title": item.get("title"), "subtitle": item.get("subtitle")}
            for item in (data.get("takeWayList") or [])
            if isinstance(item, dict)
        ]
        return cls(
            items=data.get("productList") or [],
            total=Money.from_cents(data.get("price")),
            original_total=Money.from_cents(data.get("originalPrice")),
            discount=Money.from_cents(data.get("discount")),
            take_ways=take_ways,
            server_datetime=payload.get("datetime"),
            trace_id=payload.get("traceId"),
        )


@dataclass
class StoreContext:
    """一次推荐流程所处的门店与场景上下文。"""

    store: Store
    be_type: int
    order_type: int

    @property
    def needs_be_code(self) -> bool:
        """得来速(5)、外送(2)、团餐(6) 必须带 beCode；到店自取(1) 不带。"""
        return self.be_type in (2, 5, 6)

    def price_arguments(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """构造 calculate-price 的入参。

        已实测：items 的字段名是 productCode / quantity，
        使用 code / count 会返回业务错误 code=600010。
        """
        arguments: dict[str, Any] = {
            "storeCode": self.store.store_code,
            "beType": self.be_type,
            "orderType": self.order_type,
            "items": items,
        }
        if self.needs_be_code and self.store.be_code:
            arguments["beCode"] = self.store.be_code
        return arguments


def parse_nutrition_index(payload: Any) -> dict[str, Nutrition]:
    """解析 list-nutrition-foods 的返回，建立 名称 -> 营养数据 索引。

    该工具返回的是一个自定义 DSL 字符串而非标准 JSON，形如：
        "[160]{productName,nutritionDescription,energyKj,energyKcal,...}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          ..."
    因此不能直接 json.loads，需要按行解析。返回值以名称为键；
    重名时第一条优先，避免误配。
    """
    if isinstance(payload, dict):
        payload = payload.get("data")

    if isinstance(payload, list):
        text = "\n".join(str(item) for item in payload)
    elif isinstance(payload, str):
        text = payload
    else:
        return {}

    lines = [line.strip() for line in text.splitlines()]
    header_index = next((i for i, line in enumerate(lines) if "}:" in line), None)
    if header_index is None:
        return {}

    header = lines[header_index][: lines[header_index].index("}")]
    columns = [column.strip() for column in header.split("{")[-1].split(",")]

    index: dict[str, Nutrition] = {}
    for line in lines[header_index + 1 :]:
        if not line:
            continue
        fields = line.split(",")
        if len(fields) < len(columns):
            continue
        row = dict(zip(columns, fields))
        name = (row.get("productName") or "").strip()
        if not name or name in index:
            continue

        def as_int(key: str) -> int | None:
            value = row.get(key)
            if value in (None, "", "null"):
                return None
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        index[name] = Nutrition(
            energy_kcal=as_int("energyKcal"),
            protein=as_int("protein"),
            fat=as_int("fat"),
            carbohydrate=as_int("carbohydrate"),
            sodium=as_int("sodium"),
        )
    return index


def match_nutrition(name: str, index: dict[str, Nutrition]) -> Nutrition:
    """按名称匹配营养数据。

    匹配不上就返回空的 Nutrition（全部 None）。绝不做模糊猜测，
    也不允许用「看起来像」的方式推断热量。
    """
    if not name:
        return Nutrition()

    if name in index:
        return index[name]

    normalized = name.replace("套餐", "").replace("三件套", "").replace("二件套", "").strip()
    if normalized in index:
        return index[normalized]

    # 仅接受完全包含且唯一的情况，避免误配
    matched = [value for key, value in index.items() if key and normalized and key in normalized]
    return matched[0] if len(matched) == 1 else Nutrition()