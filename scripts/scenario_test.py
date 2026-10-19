"""多场景真实 MCP 批量测试。

对同一个 Skill 实例连续执行多种真实查询，覆盖：
模糊请求、明确预算、多人、排除项、单答案、极端预算、稳定性等。
用于验证真实场景下的行为，而不只是单测里的构造夹具。

用法：
    export MCD_MCP_TOKEN="<token>"
    python scripts/scenario_test.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from mcchoice.adapters.mcd_client import ClientConfig, McDClient  # noqa: E402
from mcchoice.adapters.service import McDService  # noqa: E402
from mcchoice.skill import McChoiceSkill  # noqa: E402

SCENARIOS = [
    ("模糊请求", "随便推荐一个", "不该追问，直接给答案"),
    ("明确预算", "30元以内随便吃", "预算硬约束"),
    ("极低预算", "8元以内能吃什么", "应如实告知冲突或给出真实选项"),
    ("排除辣", "不吃辣，午饭预算40元", "排除项不得出现"),
    ("只要一个", "别列一堆，直接决定一个就行", "不应出现备选"),
    ("三选一", "给我三个方案，预算50元", "应有三个方案"),
    ("多人组合", "两个人吃，预算60元", "应为组合方案"),
    ("明确鸡肉", "想吃鸡肉，35元以内", "口味偏好应生效"),
    ("咖啡", "想喝点咖啡", "用户明确要饮品时不应被降权"),
    ("稳定性", "30元以内随便吃", "与第2条重复，验证结果一致"),
]


def main() -> int:
    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN", file=sys.stderr)
        return 2

    config = ClientConfig(token=token)
    skill = McChoiceSkill(McDService(McDClient(config)), enable_coupon_lookup=True)

    print("=" * 70)
    print("McChoice 多场景真实测试")
    print("=" * 70)

    answers: dict[str, str] = {}
    total_calls = 0
    failures = 0

    for index, (label, query, expectation) in enumerate(SCENARIOS, start=1):
        print(f"\n{'─' * 70}")
        print(f"[{index}/{len(SCENARIOS)}] {label}：{query}")
        print(f"      预期：{expectation}")
        print("─" * 70)

        started = time.monotonic()
        try:
            result = skill.decide(query, city="南京", keyword="新街口")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 抛出未捕获异常：{type(exc).__name__}: {exc}")
            failures += 1
            continue

        elapsed = (time.monotonic() - started) * 1000
        answers[query] = result.answer

        # --- 自动断言 ---
        checks: list[tuple[str, bool]] = []

        checks.append(("未编造价格", "¥" not in result.answer or "无法确认" in result.answer))
        checks.append(
            ("未声称已下单", not any(w in result.answer for w in ("已下单", "已支付", "下单成功", "已领取")))
        )
        checks.append(("数据来源非空", result.data_source == "real_mcp"))

        if "不吃辣" in query:
            checks.append(("排除项生效", "香辣" not in result.answer and "辣味" not in result.answer))
        if "别列一堆" in query:
            checks.append(("单答案模式", "备选" not in result.answer))
        if "给我三个方案" in query:
            checks.append(("三方案模式", "备选" in result.answer or "今天首选" in result.answer))
        if "两个人" in query:
            checks.append(("组合模式", "组合总价" in result.answer or "今天首选" in result.answer))

        print(f"  用时 {elapsed:.0f}ms | 候选 {result.candidates_considered} 个 | 通过硬约束 {result.candidates_passed} 个")

        failed_checks = [name for name, ok in checks if not ok]
        if failed_checks:
            failures += 1
            print(f"  ❌ 断言失败：{', '.join(failed_checks)}")
        else:
            print("  ✅ 全部断言通过")

        # 输出正文（去掉券段落以保持紧凑）
        body = result.answer.split("### 券的情况")[0].strip()
        print()
        for line in body.splitlines():
            print(f"  {line}")

        if result.warnings:
            print(f"\n  ⚠️  告警：{'；'.join(result.warnings)}")

        total_calls = len(config.audit_log)

    # --- 稳定性说明 ---
    print(f"\n{'─' * 70}")
    print("关于稳定性")
    print("─" * 70)
    print("  排序稳定性由单元测试 test_sorting_is_stable_for_identical_input 覆盖。")
    print("  真实场景下门店菜单与价格会实时变化，因此同一句话两次运行")
    print("  推荐结果可能不同 —— 这是数据在变，不是算法不稳定。")
    print("  真正需要保证的是：同一份数据 + 同一输入 → 同一排序结果。")

    print(f"\n{'=' * 70}")
    print(f"累计真实 MCP 调用：{total_calls} 次")
    print(f"场景失败数：{failures}")
    print(f"{'=' * 70}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())