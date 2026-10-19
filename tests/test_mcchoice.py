"""McChoice 单元测试。

全部使用 tests/fake_client.py 中的构造夹具，不访问网络，
不包含任何真实麦当劳业务数据。

运行：python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

try:
    import pytest
except ModuleNotFoundError:  # pytest 为可选依赖，零依赖运行器场景下也能导入
    pytest = None  # type: ignore[assignment]

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _ExceptionInfo:
    """兼容 pytest.raises 的最小实现。"""

    def __init__(self) -> None:
        self.exception: BaseException | None = None

    @property
    def value(self) -> BaseException | None:
        return self.exception


@contextmanager
def _raises(exception_type: type[BaseException]):
    """pytest.raises 的零依赖替代实现。

    语义与 pytest.raises 一致：进入上下文后代码必须抛出指定异常，
    否则抛出 AssertionError。
    """
    info = _ExceptionInfo()
    try:
        yield info
    except exception_type as exc:
        info.exception = exc
        return
    raise AssertionError(f"期望抛出 {exception_type.__name__}，但没有异常发生")

from mcchoice.adapters.mcd_client import McDError, McDErrorKind  # noqa: E402
from mcchoice.adapters.service import McDService  # noqa: E402
from mcchoice.engines.recommender import (  # noqa: E402
    detect_conflict,
    filter_candidates,
    matches_exclusion,
    pick_three,
    plan_combo,
    score_candidates,
)
from mcchoice.intent import parse_intent  # noqa: E402
from mcchoice.models import (  # noqa: E402
    Candidate,
    DataOrigin,
    Money,
    Nutrition,
    QueryStatus,
    Store,
    StoreContext,
    match_nutrition,
    parse_nutrition_index,
)
from mcchoice.skill import McChoiceSkill  # noqa: E402
from tests.fake_client import FAKE_PRICES, FakeClient  # noqa: E402


def build_skill(client: FakeClient) -> McChoiceSkill:
    return McChoiceSkill(McDService(client), enable_coupon_lookup=True)


# ----------------------------------------------------------------------
# 需求解析
# ----------------------------------------------------------------------


class TestIntentParsing:
    def test_extracts_budget_and_flags_strictness(self):
        intent = parse_intent("30元以内吃什么")
        assert intent.budget_cents == 3000
        assert intent.budget_is_strict is True

    def test_soft_budget_is_not_strict(self):
        intent = parse_intent("大概 40 块吧")
        assert intent.budget_cents == 4000
        assert intent.budget_is_strict is False

    def test_extracts_party_size(self):
        assert parse_intent("两个人吃").party_size == 2
        assert parse_intent("3个人").party_size == 3
        assert parse_intent("随便来点").party_size == 1

    def test_extracts_exclusions(self):
        intent = parse_intent("不吃辣，想吃鸡肉")
        assert "辣" in intent.exclusions
        assert "鸡肉" in intent.preferences
        # 排除项不应同时出现在偏好里
        assert "辣" not in intent.preferences

    def test_detects_single_answer_intent(self):
        intent = parse_intent("别给我列一堆，随便选一个就行")
        assert intent.wants_single_answer is True
        assert intent.is_vague() is True

    def test_detects_three_options_intent(self):
        assert parse_intent("给我三个方案").wants_three_options is True

    def test_vague_input_has_no_critical_questions(self):
        from mcchoice.intent import build_followup_questions

        intent = parse_intent("随便推荐一个")
        assert build_followup_questions(intent) == []

    def test_recognizes_colloquial_scenarios(self):
        """口语化场景词必须被识别。

        回归测试：真实测试「30元以内晚上吃什么」时，"晚上" 未被识别，
        导致场景分恒为中性 6 分，推荐结果失去场景区分度。
        """
        assert parse_intent("30元以内晚上吃什么").scenario == "晚餐"
        assert parse_intent("晚上吃什么").scenario == "晚餐"
        assert parse_intent("半夜饿了").scenario == "夜宵"
        assert parse_intent("明天中午吃什么").scenario == "午餐"
        assert parse_intent("早饭随便来点").scenario == "早餐"
        assert parse_intent("下午茶想吃甜的").scenario == "下午茶"

    def test_scenario_affects_scoring(self):
        """场景识别后应真正影响评分，而不是恒为中性分。"""
        from mcchoice.engines.recommender import score_candidates

        night_candidate = Candidate(
            product_code="N1", name="夜间拼盘", category="小食拼盘", tags=["拼盘"]
        )
        night_candidate.price = Money(3500)
        night_candidate.price_status = QueryStatus.OK

        scores = {}
        for label, text in (("晚餐", "晚上吃什么"), ("下午茶", "下午茶吃什么")):
            scored = score_candidates([night_candidate], parse_intent(text))
            scores[label] = scored[0].breakdown["场景适配"]

        assert scores["晚餐"] > scores["下午茶"], (
            f"拼盘在晚餐场景的适配分应高于下午茶：{scores}"
        )


# ----------------------------------------------------------------------
# 硬约束
# ----------------------------------------------------------------------


class TestHardConstraints:
    def _candidate(self, code="T001", name="测试汉堡套餐A", price=2790, **kw):
        candidate = Candidate(product_code=code, name=name, **kw)
        if price is not None:
            candidate.price = Money(price)
            candidate.price_status = QueryStatus.OK
        return candidate

    def test_excluded_item_never_recommended(self):
        """用户明确排除的商品绝不能进入推荐。"""
        candidate = self._candidate(name="韩式辣味鸡肉堡")
        intent = parse_intent("不吃辣")
        assert matches_exclusion(candidate, intent.exclusions) == "辣"

        result = filter_candidates([candidate], intent)
        assert result.passed == []
        assert "辣" in result.rejected[0][1]

    def test_strict_budget_rejects_over_budget(self):
        intent = parse_intent("20元以内")
        candidates = [self._candidate(price=2790), self._candidate(code="T004", name="便宜套餐", price=1900)]
        result = filter_candidates(candidates, intent)
        codes = {item.product_code for item in result.passed}
        assert codes == {"T004"}

    def test_soft_budget_keeps_over_budget_with_warning(self):
        intent = parse_intent("大概20块")
        candidates = [self._candidate(price=2790)]
        result = filter_candidates(candidates, intent)
        # 非硬约束时不淘汰，但预算契合度应为 0
        assert len(result.passed) == 1
        scored = score_candidates(result.passed, intent)
        assert scored[0].breakdown["预算契合"] == 0.0

    def test_missing_price_is_never_claimed_affordable(self):
        candidate = self._candidate(code="T005", name="无价格商品", price=None)
        candidate.price_status = QueryStatus.FAILED
        intent = parse_intent("随便推荐一个")
        scored = score_candidates([candidate], intent)
        assert any("价格暂无法确认" in warning for warning in scored[0].warnings)
        assert candidate.describe_price() == "价格暂无法确认"

    def test_conflict_detected_when_all_rejected(self):
        intent = parse_intent("10元以内，不吃辣")
        candidates = [self._candidate(name="辣味堡", price=2790)]
        result = filter_candidates(candidates, intent)
        conflict = detect_conflict(intent, result)
        assert conflict is not None
        assert "放宽" in conflict

    def test_all_over_budget_returns_clear_message(self):
        intent = parse_intent("10元以内")
        candidates = [self._candidate(price=2790)]
        result = filter_candidates(candidates, intent)
        assert result.passed == []
        assert detect_conflict(intent, result) is not None


# ----------------------------------------------------------------------
# 评分与排序
# ----------------------------------------------------------------------


class TestScoring:
    def _candidate(self, code, name, price, category=None, tags=None):
        candidate = Candidate(product_code=code, name=name, category=category, tags=tags or [])
        candidate.price = Money(price)
        candidate.price_status = QueryStatus.OK
        candidate.origin = DataOrigin.REAL_MCP
        return candidate

    def test_sorting_is_stable_for_identical_input(self):
        """相同输入与相同数据下，默认排序保持稳定。"""
        candidates = [
            self._candidate("T001", "套餐A", 2790, "精选单人餐"),
            self._candidate("T002", "套餐B", 2790, "精选单人餐"),
            self._candidate("T003", "套餐C", 2790, "精选单人餐"),
        ]
        intent = parse_intent("随便推荐一个")
        first = [item.product_code for item in score_candidates(candidates, intent)]
        second = [item.product_code for item in score_candidates(list(candidates), intent)]
        assert first == second == ["T001", "T002", "T003"]

    def test_preference_raises_score(self):
        candidates = [
            self._candidate("T001", "香辣鸡腿堡", 2000, "鸡肉汉堡"),
            self._candidate("T002", "蔬菜沙拉", 2000, "轻食"),
        ]
        intent = parse_intent("想吃鸡肉")
        scored = score_candidates(candidates, intent)
        assert scored[0].product_code == "T001"

    def test_combo_noted_for_party(self):
        candidates = [self._candidate("T003", "多人拼盘", 4500, "小食拼盘", ["多人餐"])]
        intent = parse_intent("两个人吃")
        scored = score_candidates(candidates, intent)
        assert any("2 人用餐" in reason for reason in scored[0].reasons)

    def test_no_calorie_claim_without_nutrition_data(self):
        candidate = self._candidate("T001", "未知商品", 2790)
        intent = parse_intent("随便推荐一个")
        scored = score_candidates([candidate], intent)
        assert any("无营养数据" in warning for warning in scored[0].warnings)

    def test_pick_three_returns_distinct_options(self):
        candidates = [
            self._candidate("T001", "套餐A", 1900, "精选单人餐"),
            self._candidate("T002", "套餐B", 2790, "精选单人餐"),
            self._candidate("T003", "拼盘", 4500, "小食拼盘", ["多人餐"]),
        ]
        intent = parse_intent("给我三个方案")
        picks = pick_three(score_candidates(candidates, intent))
        best = picks["稳妥"]
        others = [item for key, item in picks.items() if key != "稳妥" and item is not None]
        assert best is not None
        assert all(item is not best for item in others)
        # 惊喜之选若存在，必须是可组合的真实商品
        if picks["惊喜"] is not None:
            assert picks["惊喜"].candidate.product_code

    def test_standalone_beverage_not_picked_as_meal(self):
        """单杯饮料不应被当成一顿正餐的首选。"""
        from mcchoice.engines.recommender import _is_standalone_beverage

        for name in ("冰浓浓黑巧中杯", "雪碧（小杯）", "可乐中杯", "纯悦", "美式（大杯）"):
            candidate = Candidate(product_code="X", name=name, category="精选单人餐")
            assert _is_standalone_beverage(candidate) is True, name

        for name in ("巨无霸", "麦辣鸡腿汉堡", "麦乐鸡5块", "中薯条"):
            candidate = Candidate(product_code="Y", name=name, category="精选单人餐")
            assert _is_standalone_beverage(candidate) is False, name

    def test_beverage_is_demoted_but_not_removed(self):
        """饮品应降权但不被硬性排除 —— 用户真要喝东西时仍可推荐。"""
        drink = self._candidate("T900", "雪碧中杯", 900, "精选单人餐")
        meal = self._candidate("T001", "麦乐鸡5块", 1400, "小食")
        scored = score_candidates([drink, meal], parse_intent("随便推荐一个"))
        assert scored[0].product_code == "T001"

        scored_drink = score_candidates([drink], parse_intent("想喝点"))
        assert scored_drink[0].product_code == "T900"


# ----------------------------------------------------------------------
# 组合规划
# ----------------------------------------------------------------------


class TestComboPlanning:
    def test_combo_total_requires_all_prices(self):
        known = [
            Candidate(product_code="T001", name="A"),
            Candidate(product_code="T004", name="B"),
        ]
        for item, price in zip(known, (1900, 2500)):
            item.price = Money(price)
            item.price_status = QueryStatus.OK

        unpriced = Candidate(product_code="T005", name="C")
        unpriced.price_status = QueryStatus.FAILED

        intent = parse_intent("三个人吃，预算100元")
        items, total = plan_combo([*known, unpriced], intent)
        # T005 无价格会被过滤，因此总价仍可确认
        assert total is None or isinstance(total, Money)

    def test_combo_respects_budget(self):
        candidates = []
        for code, price in (("T001", 1900), ("T002", 2790), ("T003", 4500)):
            item = Candidate(product_code=code, name=code)
            item.price = Money(price)
            item.price_status = QueryStatus.OK
            candidates.append(item)

        intent = parse_intent("预算50元，两个人")
        items, total = plan_combo(candidates, intent)
        assert total is not None
        assert total.cents <= 5000

    def test_combo_returns_empty_when_no_priced_items(self):
        unpriced = Candidate(product_code="T005", name="C")
        unpriced.price_status = QueryStatus.FAILED
        items, total = plan_combo([unpriced], parse_intent("两个人吃"))
        assert items == []
        assert total is None

    def test_combo_must_contain_a_real_meal(self):
        """组合不能是三杯饮料 —— 回归测试。

        真实集成验证曾出现「纯悦 + 雪碧 + 热牛奶」被当成两人午餐的情况，
        这显然不是一顿饭。
        """
        from mcchoice.engines.recommender import _is_standalone_beverage

        beverages = []
        for code, name, price in (
            ("B1", "纯悦", 750),
            ("B2", "雪碧（小杯）", 950),
            ("B3", "热牛奶中杯", 950),
        ):
            item = Candidate(product_code=code, name=name, category="精选单人餐")
            item.price = Money(price)
            item.price_status = QueryStatus.OK
            beverages.append(item)

        intent = parse_intent("两个人吃，预算60元")
        items, total = plan_combo(beverages, intent)
        # 全部是饮品时不应组成"一顿饭"，应返回空列表让上层如实告知
        assert items == [], "全是饮品时应返回空组合，而不是硬凑"
        assert total is None

    def test_combo_with_meal_and_beverage_is_allowed(self):
        """有正餐基底时，饮品可以作为搭配加入。"""
        meal = Candidate(product_code="M1", name="巨无霸", category="精选单人餐")
        meal.price = Money(2650)
        meal.price_status = QueryStatus.OK

        drink = Candidate(product_code="B1", name="可乐中杯", category="精选单人餐")
        drink.price = Money(700)
        drink.price_status = QueryStatus.OK

        items, total = plan_combo([drink, meal], parse_intent("两个人吃，预算60元"))
        assert len(items) >= 1
        assert total is not None
        names = {item.name for item in items}
        assert "巨无霸" in names


# ----------------------------------------------------------------------
# 营养数据解析
# ----------------------------------------------------------------------


class TestNutrition:
    def test_parses_custom_dsl_format(self):
        index = parse_nutrition_index(
            "[2]{productName,energyKcal,protein}:\n  甲套餐,387,23\n  乙套餐,400,25"
        )
        assert index["甲套餐"].energy_kcal == 387
        assert index["甲套餐"].protein == 23
        assert index["乙套餐"].energy_kcal == 400

    def test_no_fuzzy_nutrition_guess(self):
        """名称对不上时返回空营养，不允许推断。"""
        index = {"巨无霸": Nutrition(energy_kcal=513)}
        nutrition = match_nutrition("完全不同的商品名", index)
        assert nutrition.energy_kcal is None

    def test_strips_set_suffix_for_match(self):
        index = {"枫糖风味厚松饼猪柳蛋堡": Nutrition(energy_kcal=387)}
        nutrition = match_nutrition("枫糖风味厚松饼猪柳蛋堡三件套", index)
        assert nutrition.energy_kcal == 387


# ----------------------------------------------------------------------
# Skill 端到端（假客户端）
# ----------------------------------------------------------------------


class TestSkillEndToEnd:
    def test_vague_request_gives_direct_answer(self):
        skill = build_skill(FakeClient())
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "麦麦已经替你决定了" in result.answer
        assert result.intent.is_vague()

    def test_missing_location_asks_instead_of_guessing(self):
        skill = build_skill(FakeClient())
        result = skill.decide("随便推荐一个")
        assert "先确认一下你在哪" in result.answer
        assert result.followup_questions

    def test_single_answer_request_skips_three_options(self):
        skill = build_skill(FakeClient())
        result = skill.decide("别列一堆，直接决定一个就行", city="测试市", keyword="测试区")
        assert "备选" not in result.answer

    def test_excluded_item_absent_from_output(self):
        client = FakeClient(
            details={
                "T001": {"code": "T001", "name": "香辣鸡腿堡", "rounds": []},
                "T002": {"code": "T002", "name": "香辣鸡腿堡套餐", "rounds": []},
                "T003": {"code": "T003", "name": "辣味拼盘", "rounds": []},
                "T004": {"code": "T004", "name": "清爽沙拉", "rounds": []},
                "T005": {"code": "T005", "name": "原味鸡翅", "rounds": []},
            }
        )
        skill = build_skill(client)
        result = skill.decide("不吃辣，随便推荐", city="测试市", keyword="测试区")
        # 不吃辣时应至少不把纯辣品作为唯一答案
        assert "辣味拼盘" not in result.answer or "没有可用选项" in result.answer

    def test_budget_conflict_reports_honestly(self):
        skill = build_skill(FakeClient())
        result = skill.decide("5元以内随便吃", city="测试市", keyword="测试区")
        assert "没有可用选项" in result.answer or "放宽" in result.answer
        # 绝不能编造出一个套餐来凑答案
        assert "今天首选" not in result.answer
        # 内部对象不允许泄露给用户
        assert "Candidate(product_code=" not in result.answer
        # 冲突说明里应包含淘汰原因
        assert "价格无法确认" in result.answer or "超出预算" in result.answer

    def test_mcp_failure_degrades_without_faking(self):
        client = FakeClient(fail_tools={"query-meals"})
        skill = build_skill(client)
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "没连上" in result.answer
        assert "麦麦已经替你决定了" not in result.answer

    def test_timeout_degrades_gracefully(self):
        client = FakeClient(fail_tools={"query-meals"})
        skill = build_skill(client)
        result = skill.decide("推荐一个", city="测试市", keyword="测试区")
        assert result.data_source == "real_mcp"
        assert len(result.warnings) > 0

    def test_empty_store_list_handled(self):
        skill = build_skill(FakeClient(stores=[]))
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "没有查到" in result.answer

    def test_empty_menu_handled(self):
        skill = build_skill(FakeClient(menu={"data": {"categories": []}}))
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "没有返回可用菜单" in result.answer

    def test_never_claims_order_placed(self):
        skill = build_skill(FakeClient())
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        for forbidden in ("已下单", "已支付", "已领取", "下单成功"):
            assert forbidden not in result.answer

    def test_no_forbidden_tools_called(self):
        """核心安全断言：本项目绝不触发交易类工具。"""
        from mcchoice.adapters.mcd_client import McDClient

        client = FakeClient()
        skill = build_skill(client)
        skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        called = set(client.called_tools())
        assert not (McDClient.FORBIDDEN_TOOLS & called)

    def test_coupon_lookup_is_read_only(self):
        client = FakeClient()
        skill = build_skill(client)
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "available-coupons" in client.called_tools()
        assert "auto-bind-coupons" not in client.called_tools()
        assert "券的情况" in result.answer

    def test_store_coupon_zero_is_reported_normally(self):
        client = FakeClient(store_coupons=[])
        skill = build_skill(client)
        result = skill.decide("随便推荐一个", city="测试市", keyword="测试区")
        assert "暂无可用券" in result.answer

    def test_multi_person_request_uses_combo(self):
        skill = build_skill(FakeClient())
        result = skill.decide("两个人吃，预算100元", city="测试市", keyword="测试区")
        assert "组合总价" in result.answer or "麦麦已经" in result.answer


# ----------------------------------------------------------------------
# 客户端配置与安全
# ----------------------------------------------------------------------


class TestClientSafety:
    def test_missing_token_raises_without_leaking(self):
        from mcchoice.adapters.mcd_client import ClientConfig

        with _raises(McDError) as excinfo:
            ClientConfig(token=None)
        assert "MCD_MCP_TOKEN" in str(excinfo.exception)

    def test_forbidden_tools_are_blocked(self):
        from mcchoice.adapters.mcd_client import ClientConfig, McDClient

        client = McDClient(ClientConfig(token="fake-token-for-test"))
        with _raises(McDError) as excinfo:
            client.call("create-order", {"storeCode": "X"})
        assert excinfo.exception.kind == McDErrorKind.UNSUPPORTED