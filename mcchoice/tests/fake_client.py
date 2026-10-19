"""测试专用的假 MCP 客户端。

重要：这些数据全部为构造的测试夹具，**不是真实麦当劳业务数据**，
仅用于验证逻辑分支。项目文档与演示中不会把它们当作真实数据使用。
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcchoice.adapters.mcd_client import McDError, McDErrorKind  # noqa: E402

# 构造的门店数据
FAKE_STORE = {
    "storeCode": "TEST0001",
    "storeName": "测试门店（非真实数据）",
    "address": "测试地址",
    "distance": 100,
    "businessStartTime": "07:00",
    "businessEndTime": "22:00",
    "beCode": None,
    "reservation": True,
    "reservationTimeOptions": [],
}

# 构造的菜单：只有 code 和 tags，刻意不含名称与价格，模拟真实接口行为
FAKE_MENU = {
    "data": {
        "categories": [
            {
                "name": "精选单人餐",
                "meals": [
                    {"code": "T001", "tags": ["经典精选"]},
                    {"code": "T002", "tags": ["麦金卡"]},
                ],
            },
            {
                "name": "小食拼盘\n/多人餐",
                "meals": [
                    {"code": "T003", "tags": ["拼盘", "多人餐"]},
                    {"code": "T004", "tags": ["超值"]},
                ],
            },
            {
                "name": "鸡肉汉堡\n/卷",
                "meals": [{"code": "T005", "tags": ["韩式风味"]}],
            },
        ]
    }
}

# 构造的详情数据
FAKE_DETAILS: dict[str, dict[str, Any]] = {
    "T001": {"code": "T001", "name": "测试汉堡套餐A", "rounds": []},
    "T002": {"code": "T002", "name": "测试汉堡套餐B", "rounds": []},
    "T003": {"code": "T003", "name": "测试小食拼盘", "rounds": []},
    "T004": {"code": "T004", "name": "测试超值单人餐", "rounds": []},
    "T005": {"code": "T005", "name": "测试韩式鸡肉卷", "rounds": []},
}

# 构造的价格表（分）。注意：没有覆盖全部商品，用于测试价格缺失场景。
FAKE_PRICES: dict[str, int] = {
    "T001": 2790,
    "T002": 3500,
    "T003": 4500,
    "T004": 1900,
    # T005 故意缺失，用于测试"价格无法确认"分支
}

FAKE_NUTRITION = {
    "data": (
        "[4]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
        "  测试汉堡套餐A,null,1618,387,23,21,25,846,243\n"
        "  测试汉堡套餐B,null,1800,430,25,22,30,900,250\n"
        "  测试小食拼盘,null,1500,360,20,18,28,700,120\n"
        "  测试超值单人餐,null,1400,335,18,15,26,650,110\n"
    )
}

FAKE_COUPONS_TEXT = (
    "### 麦麦省优惠券列表：\n"
    "- 优惠券标题：测试券一 \\\n  状态：可领取 \\\n"
    "- 优惠券标题：测试券二 \\\n  状态：可领取 \\\n"
)


def _wrap(data: Any, tool: str) -> dict[str, Any]:
    """构造麦当劳 MCP 的原始 JSON-RPC 响应。

    仅在 _dispatch 内部使用；call() 会像真实客户端一样把响应解析回业务 payload。
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "## Response Structure\n字段说明（模拟麦当劳 MCP 的结构说明段）\n\n"
                        "## Original Response\n"
                        + __import__("json").dumps(
                            {
                                "success": True,
                                "code": 200,
                                "message": "请求成功",
                                "datetime": "2026-01-01 12:00:00",
                                "traceId": "test-trace",
                                "data": data,
                            },
                            ensure_ascii=False,
                        )
                    ),
                }
            ]
        },
    }


def _plain_text_response(text: str) -> dict[str, Any]:
    """构造非 JSON 的纯文本响应，模拟 available-coupons 的真实返回形态。"""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }


class FakeClient:
    """模拟 McDClient 接口的假客户端。

    可配置失败、空返回、缺价格等异常场景，
    让测试不依赖网络和真实麦当劳服务。
    """

    def __init__(
        self,
        *,
        stores: list[dict[str, Any]] | None = None,
        menu: dict[str, Any] | None = None,
        prices: dict[str, int] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
        store_coupons: list[dict[str, Any]] | None = None,
        fail_tools: set[str] | None = None,
        fail_times: dict[str, int] | None = None,
    ) -> None:
        self.stores = [FAKE_STORE] if stores is None else stores
        self.menu = FAKE_MENU if menu is None else menu
        self.prices = FAKE_PRICES if prices is None else prices
        self.details = FAKE_DETAILS if details is None else details
        self.store_coupons = [] if store_coupons is None else store_coupons
        self.fail_tools = fail_tools or set()
        self.fail_times = fail_times or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> tuple[Any, Any]:
        """与真实 McDClient.call 保持完全一致的契约。

        返回 (业务 payload, 审计记录)。这里复用真实客户端的解析逻辑，
        确保测试覆盖到的解析路径与线上一致。
        """
        from mcchoice.adapters.mcd_client import CallRecord, McDClient

        arguments = arguments or {}
        self.calls.append((tool, arguments))

        remaining = self.fail_times.get(tool, 0)
        if remaining > 0:
            self.fail_times[tool] = remaining - 1
            raise McDError(McDErrorKind.TIMEOUT, f"测试用超时：{tool}", retryable=False)

        if tool in self.fail_tools:
            raise McDError(McDErrorKind.TRANSPORT, f"测试用失败：{tool}")

        record = CallRecord(tool=tool, arguments=arguments, ok=True, latency_ms=1)
        response = self._dispatch(tool, arguments)
        if isinstance(response, Exception):
            raise response

        # 用真实客户端的解析器把原始响应转成业务 payload
        payload, _server_datetime, _trace_id = McDClient._extract_payload(response)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise McDError(
                McDErrorKind.BUSINESS,
                f"麦当劳系统返回 code={payload.get('code')}：{payload.get('message')}",
                tool=tool,
            )
        return payload, record

    def _dispatch(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool == "query-nearby-stores":
            return _wrap(self.stores, tool)
        if tool == "query-meals":
            return _wrap(self.menu["data"], tool)
        if tool == "query-meal-detail":
            code = arguments.get("code")
            detail = self.details.get(code)
            if detail is None:
                return _wrap(None, tool)
            return _wrap(detail, tool)
        if tool == "calculate-price":
            return self._calculate(arguments)
        if tool == "list-nutrition-foods":
            return _wrap(FAKE_NUTRITION["data"], tool)
        if tool == "available-coupons":
            # 实测：该工具返回 Markdown 文本，不是 JSON，因此不在 _wrap 里
            return _plain_text_response(FAKE_COUPONS_TEXT)
        if tool == "query-store-coupons":
            return _wrap(self.store_coupons, tool)
        if tool == "now-time-info":
            return _wrap({"formatted": "2026-01-01 12:00:00", "date": "2026-01-01"}, tool)
        raise AssertionError(f"FakeClient 未实现的工具：{tool}")

    def _calculate(self, arguments: dict[str, Any]) -> Any:
        items = arguments.get("items") or []
        product_list = []
        total = 0
        for item in items:
            code = item.get("productCode")
            if code not in self.prices:
                # 真实服务的行为：缺价格时该商品不计入
                continue
            subtotal = self.prices[code] * int(item.get("quantity", 1))
            product_list.append(
                {
                    "productCode": code,
                    "productName": self.details.get(code, {}).get("name", code),
                    "quantity": item.get("quantity", 1),
                    "subtotal": subtotal,
                }
            )
            total += subtotal

        if not product_list:
            from mcchoice.adapters.mcd_client import McDError, McDErrorKind

            return McDError(McDErrorKind.BUSINESS, "缺少商品code或者商品数量quantity不正确", retryable=False)

        return _wrap(
            {
                "productOriginalPrice": total,
                "productPrice": total,
                "originalPrice": total,
                "discount": 0,
                "price": total,
                "productList": product_list,
                "takeWayList": [{"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"}],
            },
            "calculate-price",
        )

    def called_tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]