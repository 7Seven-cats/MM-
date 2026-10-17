"""输出格式化与异常降级。

对用户的所有承诺都必须可追溯：
- 价格要么有 calculate-price 的真实返回，要么明确说"无法确认"
- 组合要么全部价格确认，要么不给总价
- 绝不说"已下单""已用券""已支付"
"""

from __future__ import annotations

from .engines.recommender import ScoredCandidate, describe_breakdown
from .models import Candidate, Money, Store

CLOSING_LINES = (
    "麦麦已经替你决定了，剩下的是把它吃掉的力气。",
    "选择困难 cured，下一步是找座位。",
    "决定权交给你了，胃不用再开会。",
    "就这个，别再打开菜单App了。",
)


def _name(candidate: Candidate) -> str:
    return candidate.name or f"商品 {candidate.product_code}"


def format_single(
    scored: ScoredCandidate,
    *,
    store: Store | None,
    total: Money | None,
    server_time: str | None,
) -> str:
    """单一推荐输出。用户只要一个答案时使用。"""
    candidate = scored.candidate
    lines = ["### 麦麦已经替你决定了", "", f"**今天首选：{_name(candidate)}**", ""]

    price_text = (
        f"预计总价：¥{total}"
        if total is not None
        else f"预计总价：{candidate.describe_price()}"
    )
    lines.append(f"- {price_text}")

    if candidate.category:
        lines.append(f"- 品类：{candidate.category}")
    if candidate.combo_items:
        lines.append(f"- 套餐包含：{'、'.join(candidate.combo_items)}")

    lines.append(f"- 推荐理由：{describe_breakdown(scored)}")
    if scored.reasons:
        lines.append(f"- 补充说明：{'；'.join(scored.reasons)}")
    if candidate.nutrition.energy_kcal:
        lines.append(
            f"- 营养参考：约 {candidate.nutrition.energy_kcal} kcal"
            f"（来自 MCP 营养数据，仅供参考）"
        )

    evidence = [
        "商品名称与价格来自麦当劳 MCP 实时查询",
        f"门店：{store.store_name}" if store else None,
        f"查询时间：{server_time}" if server_time else None,
    ]
    lines.append(f"- 数据依据：{'，'.join(item for item in evidence if item)}")

    if scored.warnings:
        lines.append(f"- 需要说明：{'；'.join(scored.warnings)}")

    lines.append("")
    lines.append(CLOSING_LINES[0])
    return "\n".join(lines)


def format_three(
    picks: dict[str, ScoredCandidate | None],
    *,
    store: Store | None,
    server_time: str | None,
) -> str:
    """三选一输出。稳妥 / 性价比 / 惊喜。"""
    best = picks.get("稳妥")
    if best is None:
        return "当前条件下没有可用选项。"

    lines = ["### 麦麦已经替你决定了", "", f"**今天首选：{_name(best.candidate)}**", ""]

    lines.append(f"- 预计总价：{best.candidate.describe_price()}")
    lines.append(f"- 推荐理由：{describe_breakdown(best)}")
    if best.reasons:
        lines.append(f"- 为什么是这个：{'；'.join(best.reasons)}")
    if best.candidate.combo_items:
        lines.append(f"- 套餐包含：{'、'.join(best.candidate.combo_items)}")
    lines.append(f"- 适合场景：{store.store_name}" if store else "- 适合场景：你现在想吃的这一顿")

    evidence = [
        "价格来自麦当劳 MCP calculate-price 实时计算",
        f"查询时间：{server_time}" if server_time else None,
    ]
    lines.append(f"- 数据依据：{'，'.join(item for item in evidence if item)}")
    if best.warnings:
        lines.append(f"- 需要说明：{'；'.join(best.warnings)}")

    alternatives = [
        (label, item)
        for label, item in (
            ("性价比之选", picks.get("性价比")),
            ("惊喜之选", picks.get("惊喜")),
        )
        if item is not None and item is not best
    ]
    if alternatives:
        lines.extend(["", "**如果你想换个口味，还有备选：**", ""])
        for index, (label, item) in enumerate(alternatives, start=1):
            name = _name(item.candidate)
            price = item.candidate.price
            # 每档给不同的判断角度，避免三个方案的理由长得一样
            if label == "性价比之选" and price is not None and best.candidate.price is not None:
                if price.cents < best.candidate.price.cents:
                    gap = (best.candidate.price.cents - price.cents) / 100.0
                    angle = f"比首选便宜 ¥{gap:.2f}"
                else:
                    angle = "价格与首选接近"
            elif item.reasons:
                angle = item.reasons[0]
            else:
                angle = describe_breakdown(item)

            lines.append(f"{index}. **{label}**：{name}")
            lines.append(f"   - 价格：{item.candidate.describe_price()}")
            lines.append(f"   - 为什么选它：{angle}")
            if item.candidate.combo_items:
                lines.append(f"   - 套餐包含：{'、'.join(item.candidate.combo_items)}")
            if item.warnings:
                lines.append(f"   - 注意：{'；'.join(item.warnings)}")

    lines.extend(["", CLOSING_LINES[1]])
    return "\n".join(lines)


def format_combo_plan(
    items: list[Candidate],
    total: Money | None,
    *,
    store: Store | None,
    server_time: str | None,
) -> str:
    """多人 / 大预算的组合方案输出。"""
    lines = ["### 麦麦已经帮你配好了", ""]
    for candidate in items:
        lines.append(f"- **{_name(candidate)}**：{candidate.describe_price()}")
        if candidate.combo_items:
            lines.append(f"  - 含：{'、'.join(candidate.combo_items)}")

    lines.append("")
    if total is not None:
        lines.append(f"- 组合总价：**¥{total}**")
    else:
        lines.append("- 组合总价：**暂无法确认**（有商品价格未通过 MCP 验证，不做推算）")

    lines.append(
        f"- 数据依据：价格来自 calculate-price 实时计算"
        f"{f'，门店 {store.store_name}' if store else ''}"
        f"{f'，查询时间 {server_time}' if server_time else ''}"
    )
    lines.append("")
    lines.append(CLOSING_LINES[2])
    return "\n".join(lines)


def format_no_match(reason: str) -> str:
    """所有候选被淘汰时的输出。"""
    return "\n".join(
        [
            "### 这次是真选不出来",
            "",
            reason,
            "",
            "我不会为了凑答案编一个套餐给你。放宽一个条件，我马上重算。",
        ]
    )


def format_mcp_failure(note: str, *, can_ask_user: bool = True) -> str:
    """MCP 故障时的降级输出。绝不伪装成成功。"""
    lines = ["### 麦当劳数据这次没连上", "", note]
    if can_ask_user:
        lines.extend(
            [
                "",
                "你有两个选择：",
                "1. 把你手上的菜单/价格发我，我基于你给的数据帮你搭配；",
                "2. 我先给你不涉及价格的通用搭配思路，但价格需要你到店或App确认。",
            ]
        )
    return "\n".join(lines)


def format_unsupported(what: str) -> str:
    """用户询问 MCP 未开放能力时的诚实回答。"""
    return "\n".join(
        [
            f"「{what}」不在麦当劳 MCP 当前开放的工具范围内。",
            "我只能基于已开放的菜单、算价、营养、优惠券、活动、积分能力来帮你。",
            "不会为了让你满意而假装能做。",
        ]
    )


def format_coupons(titles: list[str], store_coupon_count: int | None = None) -> str:
    """优惠券说明。区分"可领"与"本门店可用"，两者不是一回事。"""
    lines = ["### 券的情况", ""]
    if titles:
        lines.append("麦麦省当前可领的券：")
        for title in titles:
            lines.append(f"- {title}")
    else:
        lines.append("当前没有查到可领取的优惠券。")

    lines.append("")
    if store_coupon_count is None:
        lines.append("本次未能确认本门店可用的券（券与门店、渠道绑定，需下单前查询）。")
    elif store_coupon_count == 0:
        lines.append("该门店当前暂无可用券，这是正常的 —— 券是门店和渠道强绑定的。")
    else:
        lines.append(f"该门店当前有 {store_coupon_count} 张可用券，下单前建议再确认一次。")
    return "\n".join(lines)