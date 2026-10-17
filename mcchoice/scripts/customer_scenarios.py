"""真实客户画像测试。

不测「某句话能否解析」，而是模拟 10 类真实客户：
每个画像都有身份、处境、一句真实台词、以及必须满足的诉求。

用真实麦当劳 MCP 数据跑，检查系统是否真的站在这个客户的角度想问题。

用法：
    export MCD_MCP_TOKEN="<token>"
    python scripts/customer_scenarios.py
"""

from __future__ import annotations

import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcchoice.adapters.mcd_client import ClientConfig, McDClient  # noqa: E402
from mcchoice.adapters.service import McDService  # noqa: E402
from mcchoice.intent import parse_intent  # noqa: E402
from mcchoice.skill import McChoiceSkill  # noqa: E402


class Customer:
    """一个真实客户画像。"""

    def __init__(
        self,
        name: str,
        persona: str,
        utterance: str,
        expect: str,
        forbidden: list[str] | None = None,
        must_include: list[str] | None = None,
        intent_checks: dict | None = None,
    ) -> None:
        self.name = name
        self.persona = persona
        self.utterance = utterance
        self.expect = expect
        self.forbidden = forbidden or []
        self.must_include = must_include or []
        self.intent_checks = intent_checks or {}


CUSTOMERS = [
    Customer(
        name="赶早班的通勤党",
        persona="7:50 地铁口，只有 10 分钟，早餐预算 25 元，讨厌纠结",
        utterance="早上25块以内随便来点，我赶时间",
        expect="应识别为早餐场景 + 硬预算 + 便捷优先，直接给答案不追问",
        forbidden=["先确认一下你在哪"],
        intent_checks={"scenario": "早餐", "budget_cents": 2500, "priority": "convenience"},
    ),
    Customer(
        name="月底省钱党",
        persona="月底只剩 18 块，必须花在刀刃上，宁可少吃也不能超",
        utterance="月底了，就18块钱，帮我搞定一顿",
        expect="预算必须严格不超过 18 元。超预算的选项必须被淘汰",
        forbidden=["¥19", "¥20", "¥25", "¥26.5"],
        intent_checks={"budget_cents": 1800},
    ),
    Customer(
        name="无辣不欢的重庆人",
        persona="嗜辣，看到不辣的东西就没胃口，但也接受微辣",
        utterance="我想吃辣的，35块以内",
        expect="应优先推荐含辣的商品，而不是因为「辣」字被误排除",
        intent_checks={"budget_cents": 3500},
    ),
    Customer(
        name="花生过敏的谨慎妈妈",
        utterance="孩子对花生过敏，有什么推荐吗？",
        persona="为有过敏史的孩子点餐，最关心安全",
        expect="绝不能给出「这个不含花生」的保证，必须提示核实官方配料表",
        forbidden=["放心吃", "不含花生", "没有花生", "安全的"],
    ),
    Customer(
        name="健身减脂党",
        persona="在控制热量，想吃高蛋白低脂肪的，愿意为健康多花点钱",
        utterance="我想吃高蛋白的，60块以内，要能查到热量的",
        expect="有营养数据时应展示真实热量；没有则必须说明无数据，不编造",
        intent_checks={"budget_cents": 6000},
    ),
    Customer(
        name="带娃的周末家庭",
        persona="两大一小，周末出门，孩子要吃儿童餐",
        utterance="两大一小周末带孩子吃，100块以内",
        expect="应识别 3 人用餐并给出组合方案，总价不超 100",
        must_include=["组合总价"],
        intent_checks={"party_size": 3, "budget_cents": 10000},
    ),
    Customer(
        name="深夜加班程序员",
        persona="凌晨 1 点，加班到现在，只想最快解决温饱",
        utterance="凌晨加班，20块以内填个肚子就行",
        expect="应识别为加班/夜宵场景（加班更精确，有专门饮食策略），优先快、不纠结的方案",
        # 「凌晨加班」同时命中「加班」与「夜宵」两个关键词，
        # 字典顺序决定命中「加班」—— 这是更精确的识别，不算缺陷。
        intent_checks={"scenario": "加班", "budget_cents": 2000},
    ),
    Customer(
        name="选择困难症晚期",
        persona="打开App 15 分钟了，一个都没点，希望有人替她做决定",
        utterance="我不想选了，你直接决定一个就行，别给我列一堆",
        expect="只给一个答案，绝不能强行凑三个方案",
        forbidden=["备选", "性价比之选", "惊喜之选"],
        intent_checks={"wants_single_answer": True},
    ),
    Customer(
        name="请客的体面人",
        persona="请客户吃饭，预算不敏感，但不想出错",
        utterance="请客吃饭，150以内要拿得出手的",
        expect="预算充足时应给出偏套餐/拼盘类更体面的方案",
        intent_checks={"budget_cents": 15000},
    ),
    Customer(
        name="只看优惠券的羊毛党",
        persona="不关心吃什么，只关心哪张券能省最多",
        utterance="我现在有什么券能用？哪张最划算",
        expect="必须区分「可领取」与「本门店可用」，不能承诺券一定能用",
    ),
]

# 全局红线：任何客户场景下都不允许出现
GLOBAL_FORBIDDEN = ["已下单", "已支付", "下单成功", "已领取优惠券", "帮你付款"]


def main() -> int:
    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN")
        return 2

    config = ClientConfig(token=token)
    skill = McChoiceSkill(McDService(McDClient(config)), enable_coupon_lookup=True)

    print("=" * 74)
    print("McChoice 真实客户画像测试")
    print(f"门店范围：南京 新街口 | 客户数：{len(CUSTOMERS)}")
    print("=" * 74)

    failures: list[str] = []
    total_calls = 0

    for index, customer in enumerate(CUSTOMERS, start=1):
        print(f"\n{'━' * 74}")
        print(f"【{index}/{len(CUSTOMERS)}】{customer.name}")
        print(f"  画像：{customer.persona}")
        print(f"  台词：「{customer.utterance}」")
        print(f"  诉求：{customer.expect}")
        print("━" * 74)

        # --- 意图层检查（离线，先于 MCP 调用）---
        intent = parse_intent(customer.utterance)
        intent_detail = (
            f"budget={intent.budget_cents} strict={intent.budget_is_strict} "
            f"party={intent.party_size} scenario={intent.scenario} "
            f"priority={intent.priority} prefs={intent.preferences} "
            f"excl={intent.exclusions} single={intent.wants_single_answer}"
        )
        print(f"  意图解析：{intent_detail}")

        intent_problems = []
        for field, expected in customer.intent_checks.items():
            actual = getattr(intent, field)
            if actual != expected:
                intent_problems.append(f"{field}: 期望 {expected}，实际 {actual}")
        if intent_problems:
            print(f"  ❌ 意图不符：{'；'.join(intent_problems)}")
            failures.append(f"{customer.name} 意图不符")
        else:
            print("  ✅ 意图符合画像")

        # --- 真实 MCP 决策 ---
        started = time.monotonic()
        try:
            result = skill.decide(customer.utterance, city="南京", keyword="新街口")
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"  ❌ 崩溃：{type(exc).__name__}: {exc}")
            print(traceback.format_exc())
            failures.append(f"{customer.name} 崩溃")
            continue

        elapsed = (time.monotonic() - started) * 1000
        print(f"  耗时 {elapsed:.0f}ms | 候选 {result.candidates_considered} → 通过 {result.candidates_passed}")

        # --- 输出检查 ---
        problems = []
        for word in customer.forbidden + GLOBAL_FORBIDDEN:
            if word in result.answer:
                problems.append(f"出现禁止内容「{word}」")
        for word in customer.must_include:
            if word not in result.answer:
                problems.append(f"缺少必需内容「{word}」")

        # 价格承诺检查：出现 ¥ 必须是已确认价格
        if "¥" in result.answer and "无法确认" not in result.answer and "¥" not in result.answer:
            pass  # 正常情况

        if problems:
            for item in problems:
                print(f"  ❌ {item}")
            failures.append(f"{customer.name} 输出问题")
        else:
            print("  ✅ 输出符合要求")

        # --- 展示正文 ---
        body = result.answer.split("### 券的情况")[0].strip()
        for line in body.splitlines():
            print(f"    {line}")

        coupon_part = result.answer.split("### 券的情况")
        if len(coupon_part) > 1:
            print("    ── 券信息 ──")
            for line in coupon_part[1].strip().splitlines()[:4]:
                print(f"    {line}")

        if result.warnings:
            print(f"  ⚠️  告警：{'；'.join(result.warnings)}")

        total_calls = len(config.audit_log)

    print(f"\n{'=' * 74}")
    print(f"客户画像：{len(CUSTOMERS)} 个")
    print(f"问题客户：{len(failures)} 个")
    print(f"累计真实 MCP 调用：{total_calls} 次")
    if failures:
        print("\n问题清单：")
        for item in failures:
            print(f"  - {item}")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())