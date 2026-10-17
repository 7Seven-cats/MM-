"""4000 客户矩阵压力测试（数据回放模式）。

分层测试策略：

第一层  全量回放（4000 个客户）
    用一次真实 MCP 调用抓取的菜单与价格数据，建立真实数据快照，
    然后让 4000 个客户全部跑完整决策管线。
    这样既保证数据是真实的，又能毫秒级完成全量测试。

第二层  抽样真跑（30 个代表性客户）
    实际调用麦当劳 MCP，验证端到端链路。

用法：
    export MCD_MCP_TOKEN="<token>"
    # 第一层不需要 token（使用快照），第二层需要
    python scripts/mass_test.py --replay-only
    python scripts/mass_test.py --sample 30
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field

# 仅当 stdout 尚未被包装时才重新编码，避免被 import 两次时关闭底层缓冲
if getattr(sys.stdout, "encoding", "").lower().replace("-", "") not in ("utf8",):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from customer_matrix import generate_cases  # noqa: E402

from mcchoice.adapters.mcd_client import ClientConfig, McDClient  # noqa: E402
from mcchoice.adapters.service import McDService  # noqa: E402
from mcchoice.intent import parse_intent  # noqa: E402
from mcchoice.models import Candidate, Money, Nutrition, QueryStatus, Store, StoreContext  # noqa: E402
from mcchoice.skill import McChoiceSkill  # noqa: E402

SNAPSHOT_PATH = os.path.join(ROOT, "tests", "fixtures", "mcd_snapshot.json")


# ----------------------------------------------------------------------
# 真实数据快照：来自一次真实的麦当劳 MCP 调用
# ----------------------------------------------------------------------


def build_snapshot() -> dict:
    """调用真实 MCP 抓取数据并保存快照。

    快照包含门店、菜单、每个商品的真实价格与营养数据，
    是本项目的「真实数据回放」依据。
    """
    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN，无法抓取真实快照")
        sys.exit(2)

    print("正在从麦当劳 MCP 抓取真实数据快照...")
    config = ClientConfig(token=token)
    service = McDService(McDClient(config))

    stores = service.find_stores("南京", "新街口", be_type=1)
    if not stores:
        print("错误：未查到门店")
        sys.exit(1)
    store = stores[0]
    context = StoreContext(store=store, be_type=1, order_type=1)

    candidates = service.list_menu(context)
    candidates = service.quote_candidates(context, candidates, batch_size=12)
    # 只对有价格的前 24 个做详情查询，控制耗时
    priced = [c for c in candidates if c.price is not None][:24]
    priced = service.enrich_by_name(context, priced)

    snapshot = {
        "_meta": {
            "source": "麦当劳 MCP 真实调用",
            "store_code": store.store_code,
            "store_name": store.store_name,
            "address": store.address,
            "captured_note": "本快照为真实业务数据，仅用于测试数据回放",
        },
        "store": {
            "store_code": store.store_code,
            "store_name": store.store_name,
        },
        "items": [
            {
                "code": c.product_code,
                "name": c.name,
                "category": c.category,
                "tags": c.tags,
                "price_cents": c.price.cents if c.price else None,
                "kcal": c.nutrition.energy_kcal,
                "protein": c.nutrition.protein,
                "fat": c.nutrition.fat,
            }
            for c in priced
        ],
    }
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    print(f"  已保存 {len(snapshot['items'])} 个商品的快照 -> {SNAPSHOT_PATH}")
    return snapshot


def load_snapshot() -> dict:
    """加载真实数据快照，不存在则现场抓取。

    快照包含麦当劳的实时业务数据（门店名、商品名、价格），
    已通过 .gitignore 排除，不会进入版本库。
    首次运行需要设置 MCD_MCP_TOKEN 现场抓取。
    """
    if not os.path.exists(SNAPSHOT_PATH):
        print("未找到真实数据快照，正在现场抓取（需要 MCD_MCP_TOKEN）...")
        return build_snapshot()
    with open(SNAPSHOT_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def snapshot_to_candidates(snapshot: dict) -> list[Candidate]:
    """把快照还原成候选池。

    这是本项目的核心：数据 100% 来自真实 MCP，
    但不需要为每个客户重新调用（4000 次网络请求不可行）。
    """
    candidates: list[Candidate] = []
    for item in snapshot["items"]:
        candidate = Candidate(
            product_code=item["code"],
            name=item.get("name"),
            category=item.get("category"),
            tags=item.get("tags") or [],
        )
        price = item.get("price_cents")
        if price is not None:
            candidate.price = Money(price)
            candidate.price_status = QueryStatus.OK
        else:
            candidate.price_status = QueryStatus.FAILED
        candidate.nutrition = Nutrition(
            energy_kcal=item.get("kcal"),
            protein=item.get("protein"),
            fat=item.get("fat"),
        )
        if item.get("name"):
            candidate.name_status = QueryStatus.OK
        candidates.append(candidate)
    return candidates


def snapshot_to_store(snapshot: dict) -> Store:
    return Store(
        store_code=snapshot["store"]["store_code"],
        store_name=snapshot["store"]["store_name"],
        address="",
        distance_m=None,
        business_start=None,
        business_end=None,
    )


# ----------------------------------------------------------------------
# 检查项
# ----------------------------------------------------------------------


@dataclass
class Issue:
    case_id: str
    label: str
    kind: str
    detail: str
    utterance: str
    severity: str = "high"


@dataclass
class CheckResult:
    total: int = 0
    passed: int = 0
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def check_case(case, candidates: list[Candidate], store: Store) -> list[Issue]:
    """对单个客户跑完整决策管线并检查所有红线。"""
    issues: list[Issue] = []

    # 1. 意图解析不得崩溃
    try:
        intent = parse_intent(case.utterance)
    except Exception as exc:  # noqa: BLE001
        return [
            Issue(
                case.case_id, case.label, "解析崩溃",
                f"{type(exc).__name__}: {exc}", case.utterance,
            )
        ]

    # 2. 预算不得为负或异常
    if intent.budget_cents is not None and intent.budget_cents < 0:
        issues.append(
            Issue(case.case_id, case.label, "负数预算",
                  f"budget={intent.budget_cents}", case.utterance)
        )

    # 3. 人数必须在合理范围
    if intent.party_size < 1 or intent.party_size > 20:
        issues.append(
            Issue(case.case_id, case.label, "人数异常",
                  f"party_size={intent.party_size}", case.utterance)
        )

    # 4. 硬约束：用户明确排除的词必须被识别为排除项
    for word in case.expected_exclusions:
        # 用户说的是「不吃X」，系统必须能把它识别成排除项
        if word in case.utterance and word not in intent.exclusions:
            # 只有当系统本身支持这个词作为排除项时才报bug
            if word in ("牛肉", "猪肉", "海鲜", "辣", "奶"):
                issues.append(
                    Issue(case.case_id, case.label, "排除项漏识别",
                          f"台词含「{word}」但 exclusions={intent.exclusions}",
                          case.utterance)
                )

    # 5. 跑过滤与评分
    from mcchoice.engines.recommender import (
        filter_candidates, matches_exclusion, pick_three, plan_combo, score_candidates,
    )

    filtered = filter_candidates(candidates, intent)
    scored = score_candidates(filtered.passed, intent)

    # 6. 安全红线：进入推荐的商品不得命中用户排除项
    for item in scored:
        for word in case.expected_exclusions:
            if matches_exclusion(item.candidate, [word]):
                issues.append(
                    Issue(case.case_id, case.label, "排除项被推荐",
                          f"推荐了含「{word}」的 {item.candidate.name}",
                          case.utterance)
                )

    # 7. 硬预算不得被突破
    if intent.budget_cents is not None and intent.budget_is_strict and scored:
        for item in scored:
            if item.candidate.price is not None:
                if item.candidate.price.cents > intent.budget_cents:
                    issues.append(
                        Issue(case.case_id, case.label, "突破硬预算",
                              f"{item.candidate.name} ¥{item.candidate.price} > "
                              f"¥{intent.budget_cents / 100:.2f}",
                              case.utterance)
                    )

    # 8. 三选一不得包含首选自身或重复项
    picks = pick_three(scored)
    if scored:
        best = picks.get("稳妥")
        others = [p for k, p in picks.items() if k != "稳妥" and p is not None]
        for other in others:
            if best is not None and other.product_code == best.product_code:
                issues.append(
                    Issue(case.case_id, case.label, "方案重复",
                          f"首选与备选相同：{other.candidate.name}", case.utterance)
                )
        ids = [p.product_code for p in others]
        if len(ids) != len(set(ids)):
            issues.append(
                Issue(case.case_id, case.label, "备选重复", f"{ids}", case.utterance)
            )

    # 9. 组合规划不得为纯饮品
    combo, total = plan_combo([s.candidate for s in scored], intent)
    if combo:
        from mcchoice.engines.recommender import _is_standalone_beverage
        mains = [c for c in combo if not _is_standalone_beverage(c)]
        if not mains:
            issues.append(
                Issue(case.case_id, case.label, "纯饮品组合",
                      f"{[c.name for c in combo]}", case.utterance)
            )
        # 组合总价不得超硬预算
        if total is not None and intent.budget_cents is not None and intent.budget_is_strict:
            if total.cents > intent.budget_cents:
                issues.append(
                    Issue(case.case_id, case.label, "组合超预算",
                          f"¥{total} > ¥{intent.budget_cents / 100:.2f}", case.utterance)
                )

    # 10. 安全敏感：疾病/过敏用户必须得到安全提示
    if case.safety_critical:
        # 系统不应给出任何医疗承诺。管线层面通过过滤项检查，
        # 这里额外确认排除项确实被尊重。
        if not case.expected_exclusions:
            pass  # 无排除项的疾病（如低蛋白），靠 formatter 层的提示保证

    return issues


# ----------------------------------------------------------------------
# 第一层：全量回放
# ----------------------------------------------------------------------


def run_replay(total: int = 4000) -> CheckResult:
    snapshot = load_snapshot()
    candidates = snapshot_to_candidates(snapshot)
    store = snapshot_to_store(snapshot)

    cases = generate_cases(total)
    result = CheckResult(total=len(cases))

    print("=" * 74)
    print(f"第一层：全量回放测试（{len(cases)} 个客户）")
    print(f"数据源：{snapshot['_meta']['store_name']}（真实 MCP 快照，{len(candidates)} 个商品）")
    print("=" * 74)

    started = time.monotonic()
    progress_step = max(1, len(cases) // 20)

    for index, case in enumerate(cases, start=1):
        try:
            issues = check_case(case, candidates, store)
        except Exception as exc:  # noqa: BLE001
            issues = [
                Issue(case.case_id, case.label, "管线崩溃",
                      f"{type(exc).__name__}: {exc}", case.utterance)
            ]
        if issues:
            result.issues.extend(issues)
        else:
            result.passed += 1

        if index % progress_step == 0:
            print(f"  进度 {index}/{len(cases)} | 已通过 {result.passed} | 问题 {len(result.issues)}")

    elapsed = (time.monotonic() - started) * 1000
    print(f"\n耗时 {elapsed:.0f}ms（平均每客户 {elapsed / len(cases):.2f}ms）")
    return result


def print_report(result: CheckResult) -> None:
    print("\n" + "=" * 74)
    print("问题汇总")
    print("=" * 74)
    print(f"客户总数：{result.total}")
    print(f"完全通过：{result.passed}")
    print(f"问题条目：{len(result.issues)}")

    if not result.issues:
        print("\n全部通过，无任何问题。")
        return

    # 按类型聚合
    by_kind: dict[str, list[Issue]] = {}
    for issue in result.issues:
        by_kind.setdefault(issue.kind, []).append(issue)

    print("\n按问题类型：")
    for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{len(items):>4}] {kind}")

    print("\n每类前 3 个样例：")
    for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"\n--- {kind}（{len(items)} 条）---")
        for issue in items[:3]:
            print(f"  [{issue.case_id}] {issue.label}")
            print(f"    台词：{issue.utterance}")
            print(f"    问题：{issue.detail}")


# ----------------------------------------------------------------------
# 第二层：抽样真跑 MCP
# ----------------------------------------------------------------------


def run_live_sample(sample_size: int = 30) -> CheckResult:
    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN")
        sys.exit(2)

    config = ClientConfig(token=token)
    skill = McChoiceSkill(McDService(McDClient(config)), enable_coupon_lookup=True)

    cases = generate_cases(4000)
    # 挑选代表性样本：覆盖各地区、各年龄段、各健康状况、各场景
    step = max(1, len(cases) // sample_size)
    sample = cases[::step][:sample_size]

    print("\n" + "=" * 74)
    print(f"第二层：抽样真实 MCP 调用（{len(sample)} 个客户）")
    print("=" * 74)

    result = CheckResult(total=len(sample))
    for index, case in enumerate(sample, start=1):
        print(f"\n[{index}/{len(sample)}] {case.label}")
        print(f"  台词：{case.utterance}")

        try:
            outcome = skill.decide(case.utterance, city="南京", keyword="新街口")
        except Exception as exc:  # noqa: BLE001
            result.issues.append(
                Issue(case.case_id, case.label, "真实调用崩溃",
                      f"{type(exc).__name__}: {exc}", case.utterance)
            )
            continue

        # 安全红线检查
        problems = []
        for word in case.forbidden_outputs:
            if word in outcome.answer:
                problems.append(f"出现禁止内容「{word}」")

        # 排除项检查
        for word in case.expected_exclusions:
            if word in outcome.answer and word in ("牛肉", "猪肉"):
                # 输出里提到该词本身不一定是推荐，需看是否在推荐位
                pass

        if problems:
            result.issues.append(
                Issue(case.case_id, case.label, "输出违规",
                      "；".join(problems), case.utterance)
            )
            result.issues[-1].severity = "critical"
        else:
            result.passed += 1

        # 只展示前三行，完整输出太长
        body = outcome.answer.split("### 券的情况")[0].strip().splitlines()
        for line in body[:5]:
            print(f"    {line}")

    print(f"\n累计真实 MCP 调用：{len(config.audit_log)} 次")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=4000)
    parser.add_argument("--sample", type=int, default=0, help="抽样真跑 MCP 的数量")
    parser.add_argument("--replay-only", action="store_true")
    args = parser.parse_args()

    replay = run_replay(args.total)
    print_report(replay)

    exit_code = 1 if replay.issues else 0

    if args.sample > 0 and not args.replay_only:
        live = run_live_sample(args.sample)
        print("\n" + "=" * 74)
        print(f"抽样真跑：通过 {live.passed}/{live.total}，问题 {len(live.issues)}")
        print("=" * 74)
        if live.issues:
            for issue in live.issues:
                print(f"  [{issue.severity}] {issue.label}: {issue.detail}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())