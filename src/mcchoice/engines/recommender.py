"""约束过滤与推荐排序引擎。

核心原则：硬约束永远优先于总分。

无论候选的评分多高，只要用户明确排除过、或会突破明确的预算上限，
就不能进入推荐。评分只用于在"合法候选"之间排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..intent import DiningIntent
from ..models import Candidate, Money, QueryStatus

# 初始权重。这些是设计参数，不是客观事实，可在 config 中调整。
DEFAULT_WEIGHTS: dict[str, float] = {
    "requirement_match": 0.40,
    "budget_fit": 0.30,
    "taste_match": 0.20,
    "scenario_fit": 0.10,
}

# 参与人数与单人预算换算时使用的粗略依据：单人正餐的合理区间（分）。
SINGLE_MEAL_MIN_CENTS = 1500
SINGLE_MEAL_MAX_CENTS = 8000

# 多人场景的规模加成：人数越多，单人预算容忍度越高。
SCALE_FACTOR = 1.35

# 场景关键词与套餐标签/品类的匹配表。
_SCENARIO_HINTS: dict[str, tuple[str, ...]] = {
    "早餐": ("早餐", "麦满分", "贝果", "汉堡", "咖啡", "美式"),
    "午餐": ("午餐", "套餐", "三件套", "汉堡"),
    "晚餐": ("晚餐", "套餐", "三件套", "拼盘"),
    "夜宵": ("夜宵", "宵夜", "小食", "薯条", "鸡翅", "麦乐鸡"),
    "宵夜": ("夜宵", "宵夜", "小食", "薯条", "鸡翅", "麦乐鸡"),
    "加班": ("夜宵", "宵夜", "咖啡", "美式", "小食"),
    "下午茶": ("下午茶", "甜品", "新地", "麦旋风", "冰淇淋", "蛋糕"),
    "通勤": ("早餐", "咖啡", "美式", "汉堡", "小食"),
    "休闲": ("小食", "拼盘", "甜品", "麦旋风"),
    "聚会": ("拼盘", "多人餐", "分享", "家庭桶"),
    "独食": ("单人", "精选", "小食"),
}

# 单杯饮料/甜品不算一顿正餐。
# 当用户没有明确表示只想喝点什么时，这些品类不应被当成首选答案 ——
# 推荐「雪碧小杯」当午饭是荒谬的。
#
# 饮品名称识别基于实测返回的真实商品名与品类名，包括：
# 可乐、雪碧、雪碧冰爽、纯悦、阳光橙、阳光柠檬、鲜煮咖啡、美式、拿铁、
# 红茶、绿茶、奶茶、玉米杯、优酸乳、益菌多、鲜奶、特级浓滑奶昔 等。
_DRINK_ONLY_HINTS = (
    "饮料", "可乐", "雪碧", "橙汁", "美式", "咖啡", "拿铁", "奶茶", "茶饮",
    "纯悦", "阳光橙", "阳光柠檬", "冰爽", "红茶", "绿茶", "优酸乳", "益菌多",
    "鲜奶", "浓滑", "奶昔", "热饮", "冷饮", "柠檬红茶",
)
_DESSERT_ONLY_HINTS = (
    "甜品", "新地", "麦旋风", "冰淇淋", "派", "蛋糕", "布丁", "派", "蛋挞",
)


def _is_dessert(candidate: Candidate) -> bool:
    """判断是否是高糖甜品。

    低蛋白饮食、糖尿病、控糖类用户不应该在正餐搭配里收到甜品。
    这类诉求被真实抽样测试发现：低蛋白用户收到了圆筒冰淇淋。
    """
    text = _taste_text(candidate)
    if not text:
        return False
    return any(hint in text for hint in _DESSERT_ONLY_HINTS)


# 明确不该出现在这些诉求的正餐搭配里的品类
_AVOID_FOR_HEALTH_PREF = ("低蛋白", "低卡", "轻食", "低脂")


def _is_standalone_beverage(candidate: Candidate) -> bool:
    """判断候选是否是"单杯饮料/甜品"，即不足以构成一餐。

    判定策略（基于实测商品名）：
    1. 若含正餐特征词（汉堡/鸡/薯/套餐等），一定不是单杯饮品；
    2. 若含饮品关键词，或名称带「杯」量词（中杯/小杯/大杯/杯装），
       且不含正餐词，则判定为单杯饮品。

    这样「冰浓浓黑巧中杯」这类不 含已知饮品关键词、但带杯量词的商品
    也能被正确识别。
    """
    text = _taste_text(candidate)
    if not text:
        return False

    has_meal_word = any(
        hint in text
        for hint in ("汉堡", "鸡", "堡", "套餐", "拼盘", "薯", "麦满分", "卷", "饭", "面", "翅", "猪柳蛋")
    )
    if has_meal_word:
        return False

    # 杯量词是单杯饮品的强特征
    has_cup_measure = any(token in text for token in ("中杯", "小杯", "大杯", "杯装", "杯"))
    has_drink_word = any(hint in text for hint in _DRINK_ONLY_HINTS)
    has_dessert_word = any(hint in text for hint in _DESSERT_ONLY_HINTS)

    return has_cup_measure or has_drink_word or has_dessert_word


@dataclass
class ScoredCandidate:
    """带评分与评分明细的候选。

    breakdown 用于向用户解释"为什么推荐它"，
    每个维度都必须能追溯到真实字段，不允许出现凭空判断。
    """

    candidate: Candidate
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def product_code(self) -> str:
        return self.candidate.product_code

    @property
    def price(self) -> Money | None:
        return self.candidate.price


@dataclass
class FilterResult:
    """过滤结果，同时保留被淘汰的原因，用于向用户解释冲突。"""

    passed: list[Candidate]
    rejected: list[tuple[Candidate, str]]

    @property
    def rejection_summary(self) -> str:
        """被淘汰候选的简要说明。只展示商品名与原因，不暴露内部对象。"""
        parts = [
            f"{_display_name(candidate)}（{reason}）"
            for candidate, reason in self.rejected[:3]
        ]
        return "；".join(parts)


def _display_name(candidate: Candidate) -> str:
    return candidate.name or f"商品 {candidate.product_code}"


def effective_budget(intent: DiningIntent) -> Money | None:
    """计算可用于筛选的总预算。

    用户给了总预算就直接用；没给但给了人数时，
    按人数推算一个合理的总预算区间上限用于软排序。
    注意：推算值只用于排序参考，不会被当作"用户明确的预算上限"，
    也不会在输出中声称是用户预算。
    """
    if intent.budget_cents is not None:
        return Money(intent.budget_cents)
    return None


def _taste_text(candidate: Candidate) -> str:
    """把候选的品类、标签、名称、套餐子项拼成可匹配的文本。"""
    parts = [candidate.category or "", candidate.name or "", *candidate.tags]
    parts.extend(candidate.combo_items)
    return " ".join(part for part in parts if part)


# 食材同义词映射：麦当劳商品名称里的用词与用户口语往往不同。
# 例：用户说「不吃奶」（乳糖不耐），但商品名写的是「吉士」「芝士」；
#     用户说「不吃猪肉」，但商品名写的是「猪柳」「培根」。
# 缺少这张表会直接导致过敏/忌口用户收到不该吃的商品。
#
# 键是用户排除项（规范名），值是该食材在麦当劳菜单里的常见用词。
_EXCLUSION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "奶": ("奶", "牛奶", "乳", "芝士", "吉士", "起司", "奶昔", "酸奶", "布丁", "冰淇淋", "麦旋风"),
    "海鲜": ("海鲜", "虾", "蟹", "贝", "鱿鱼", "鱼", "鳕鱼"),
    "坚果": ("坚果", "花生", "杏仁", "核桃", "腰果", "榛子"),
    "牛肉": ("牛肉", "牛柳", "牛排", "巨无霸", "吉士汉堡", "双层吉士"),
    "猪肉": ("猪肉", "猪柳", "猪", "培根", "火腿", "猪柳蛋"),
    "麸质": ("麸质", "面筋", "小麦"),
    "辣": ("辣", "辣椒", "香辣", "韩式辣椒", "微辣"),
}


def matches_exclusion(candidate: Candidate, exclusions: list[str]) -> str | None:
    """判断候选是否命中用户排除项，返回命中的排除词。

    只做文本匹配，且仅用于"排除"方向 —— 即宁可多排除，
    也不要把用户明确说不要的东西推上去。
    命中过敏相关关键词时额外附加警告。
    """
    haystack = _taste_text(candidate)
    for exclusion in exclusions:
        if not exclusion:
            continue
        # 先看规范名本身，再看同义词（如「奶」→ 芝士 / 吉士 / 奶昔）
        terms = (exclusion,) + _EXCLUSION_SYNONYMS.get(exclusion, ())
        for term in terms:
            if term in haystack:
                return exclusion
    return None


def matches_preference(candidate: Candidate, preference: str) -> bool:
    return bool(preference) and preference in _taste_text(candidate)


def filter_candidates(
    candidates: list[Candidate],
    intent: DiningIntent,
    *,
    strict_budget: bool | None = None,
) -> FilterResult:
    """应用硬约束过滤。

    strict_budget 为 True 时，只要预算超了就淘汰；
    为 False（默认，跟随用户是否用了硬约束词）时，超预算候选会被
    标记但不淘汰，以支持"稍微超一点但明显更好"的场景。
    """
    use_strict = intent.budget_is_strict if strict_budget is None else strict_budget
    budget = effective_budget(intent)

    passed: list[Candidate] = []
    rejected: list[tuple[Candidate, str]] = []

    for candidate in candidates:
        # 硬约束 1：用户明确排除的商品一律不进推荐
        hit = matches_exclusion(candidate, intent.exclusions)
        if hit:
            rejected.append((candidate, f"命中你排除的「{hit}」"))
            continue

        # 硬约束 2：明确的预算上限不得被突破
        if budget is not None:
            if candidate.price is None:
                # 价格未确认时无法证明它在预算内，因此不能作为"预算内选项"推荐。
                # 非硬约束场景保留它，但预算契合度会给 0 分并在输出中标注。
                if use_strict:
                    rejected.append((candidate, "价格无法确认，无法验证是否在预算内"))
                    continue
            elif use_strict and candidate.price.cents > budget.cents:
                rejected.append((candidate, f"超出预算 ¥{budget}"))
                continue

        passed.append(candidate)

    return FilterResult(passed=passed, rejected=rejected)


def _budget_fit_score(
    candidate: Candidate, intent: DiningIntent, budget: Money | None
) -> tuple[float, str | None]:
    """预算契合度评分。

    价格未确认时返回中性分并附带说明 —— 绝不因为"没价格"
    就假设它便宜或便宜到爆。
    """
    if budget is None:
        if candidate.price is None:
            return 0.5, "价格暂无法确认，暂不参与价格比较"
        if intent.priority == "price":
            return min(1.0, 0.35 + (1 - candidate.price.cents / (SINGLE_MEAL_MAX_CENTS * SCALE_FACTOR)) * 0.65), None
        return 0.6, None

    if candidate.price is None:
        return 0.5, "价格暂无法确认，无法验证是否在预算内"

    ratio = candidate.price.cents / budget.cents if budget.cents else 1.0
    if ratio > 1.0:
        return 0.0, None
    # 预算利用率在 0.55~0.95 之间得分最高：既能吃好又不浪费
    if 0.55 <= ratio <= 0.95:
        return 1.0, None
    if ratio < 0.55:
        return 0.55 + ratio * 0.4, "价格明显低于预算"
    return 0.95 - (ratio - 0.95) * 0.3, None


# 营养类偏好的判定阈值。阈值来自 list-nutrition-foods 返回数据的实际分布，
# 不是医学标准：本项目只做「谁更符合诉求」的相对比较，不给健康建议。
# 注意字段名必须与 models.Nutrition 完全一致（energy_kcal，不是 energy）。
# 写错会 getattr 到 None，导致所有营养偏好静默退化成最低分。
_NUTRITION_PREF_RULES: dict[str, str] = {
    "高蛋白": "protein",
    "低脂": "fat",
    "低卡": "energy_kcal",
    "轻食": "energy_kcal",
    "饱腹": "energy_kcal",
}


def _nutrition_match(candidate: Candidate, preference: str) -> float:
    """用真实营养数据判断候选是否符合营养类偏好，返回 0~1 的匹配度。

    没有营养数据时返回 0.5（中性），并由调用方在 warnings 中标注，
    绝不因为「不知道」就当成「不满足」或「满足」。
    """
    field_name = _NUTRITION_PREF_RULES.get(preference)
    if field_name is None:
        return 0.0  # 非营养类偏好，交给文本匹配处理

    nutrition = candidate.nutrition
    value = getattr(nutrition, field_name, None)
    if value is None:
        return 0.5  # 无数据，保持中性

    if preference == "高蛋白":
        # 蛋白质 >= 15g 视为高蛋白（实测麦当劳常见单品多在 5~27g）
        return 1.0 if value >= 15 else (0.3 if value >= 8 else 0.0)
    if preference == "低脂":
        # 脂肪 <= 10g 视为低脂
        return 1.0 if value <= 10 else (0.4 if value <= 18 else 0.0)
    if preference in ("低卡", "轻食"):
        # 热量 <= 250 kcal 视为低卡
        return 1.0 if value <= 250 else (0.4 if value <= 400 else 0.0)
    if preference == "饱腹":
        # 「饱腹」在这里等价于热量密度高，不做医学判断
        return 1.0 if value >= 400 else (0.5 if value >= 250 else 0.1)
    return 0.5


def _taste_score(candidate: Candidate, intent: DiningIntent) -> float:
    """口味与健康取向的匹配度。

    硬约束已在过滤阶段处理（排除项不会走到这里），
    因此这里只计算正向偏好命中率。

    营养类偏好（高蛋白/低脂/低卡/饱腹）不能只看商品名称——
    麦当劳的商品名里不会写「高蛋白」。因此这类偏好改用
    list-nutrition-foods 返回的真实营养数据判定。
    """
    if not intent.preferences:
        return 0.6  # 无偏好时给中性偏高分，避免无差别惩罚

    scores: list[float] = []
    for preference in intent.preferences:
        if preference in _NUTRITION_PREF_RULES:
            scores.append(_nutrition_match(candidate, preference))
        else:
            scores.append(1.0 if matches_preference(candidate, preference) else 0.0)

    return sum(scores) / len(scores)


def _scenario_score(candidate: Candidate, intent: DiningIntent) -> float:
    """场景适配度。仅基于 MCP 返回的品类名、标签与套餐子项。"""
    if not intent.scenario:
        return 0.6
    hints = _SCENARIO_HINTS.get(intent.scenario)
    if not hints:
        return 0.6
    haystack = _taste_text(candidate)
    return 1.0 if any(hint in haystack for hint in hints) else 0.35


def _requirement_score(candidate: Candidate, intent: DiningIntent) -> tuple[float, list[str]]:
    """需求匹配度，覆盖人数与饱腹度相关的显式要求。"""
    reasons: list[str] = []
    score = 0.6

    # 单杯饮料/甜品不足以构成一餐，除非用户明确只想喝点什么
    if _is_standalone_beverage(candidate) and not intent.wants_beverage:
        score -= 0.35
        reasons.append("这是一杯饮料/甜品，如果你只想吃点东西可以忽略")

    if intent.appetite == "很饿":
        if any(hint in _taste_text(candidate) for hint in ("拼盘", "三件套", "多人餐", "大薯", "全家桶")):
            score += 0.4
            reasons.append("分量上更适合 hungry 的胃口")
        else:
            score -= 0.05
    elif intent.appetite == "不饿":
        if any(hint in _taste_text(candidate) for hint in ("小食", "甜品", "麦旋风", "新地", "咖啡")):
            score += 0.4
            reasons.append("不太饿时，小份更合适")

    if intent.party_size > 1:
        if any(hint in _taste_text(candidate) for hint in ("拼盘", "多人餐", "分享", "家庭桶")):
            score += 0.35
            reasons.append(f"{intent.party_size} 人用餐，拼盘类更合适")
        else:
            score -= 0.1

    if intent.priority == "convenience":
        if any(hint in _taste_text(candidate) for hint in ("套餐", "三件套", "二件套")):
            score += 0.25
            reasons.append("一次点齐，少纠结")

    return max(0.0, min(1.0, score)), reasons


def score_candidates(
    candidates: list[Candidate], intent: DiningIntent, weights: dict[str, float] | None = None
) -> list[ScoredCandidate]:
    """对合法候选打分排序。

    排序稳定性：分数相同时按 productCode 字典序排列，
    保证相同输入与相同数据下结果稳定可复现（随机模式除外）。
    """
    active = weights or DEFAULT_WEIGHTS
    budget = effective_budget(intent)
    scored: list[ScoredCandidate] = []

    for candidate in candidates:
        requirement, reasons = _requirement_score(candidate, intent)
        budget_fit, budget_note = _budget_fit_score(candidate, intent, budget)
        taste = _taste_score(candidate, intent)
        scenario = _scenario_score(candidate, intent)

        breakdown = {
            "需求匹配": requirement * active["requirement_match"],
            "预算契合": budget_fit * active["budget_fit"],
            "口味匹配": taste * active["taste_match"],
            "场景适配": scenario * active["scenario_fit"],
        }
        total = sum(breakdown.values())

        warnings: list[str] = []
        if candidate.price_status is not QueryStatus.OK:
            warnings.append("价格未通过 MCP 确认，无法验证预算")
        if candidate.nutrition.energy_kcal is None:
            warnings.append("无营养数据，不对热量做判断")
        if budget_note:
            warnings.append(budget_note)

        scored.append(
            ScoredCandidate(
                candidate=candidate,
                score=round(total, 6),
                breakdown=breakdown,
                reasons=reasons,
                warnings=warnings,
            )
        )

    scored.sort(key=lambda item: (-item.score, item.product_code))
    return scored


def pick_three(scored: list[ScoredCandidate]) -> dict[str, ScoredCandidate | None]:
    """生成稳妥 / 性价比 / 惊喜三档。

    - 稳妥之选：综合分最高，尽量贴合显式需求
    - 性价比之选：在价格确认的前提下，按「需求分 / 价格」效率排序，
      而不是简单取第二名 —— 否则三个方案的理由会长得一模一样。
    - 惊喜之选：口味或场景分突出，且与首选不同，组合必须由真实商品构成

    若用户明确只要一个答案，只返回首选。
    """
    if not scored:
        return {"稳妥": None, "性价比": None, "惊喜": None}

    best = scored[0]
    result: dict[str, ScoredCandidate | None] = {"稳妥": best, "性价比": None, "惊喜": None}

    def _value_ratio(item: ScoredCandidate) -> float:
        """需求分 / 价格。每元换到的满足度，越高越划算。

        价格未确认的候选不参与性价比评选（无法计算就等于不知道划不划算）。
        """
        if item.candidate.price is None or item.candidate.price.cents <= 0:
            return -1.0
        return item.score / (item.candidate.price.cents / 100.0)

    value_candidates = [
        item
        for item in scored
        if item is not best
        and item.candidate.has_confirmed_price()
        and not _is_standalone_beverage(item.candidate)
    ]
    value_candidates.sort(
        key=lambda item: (-_value_ratio(item), item.candidate.price.cents, item.product_code)
    )

    value_pick = next(iter(value_candidates), None)
    if value_pick is None:
        # 所有备选都没有确认价格，或都是饮品：退化为取第二个
        value_pick = next(
            (item for item in scored if item is not best),
            None,
        )
    result["性价比"] = value_pick

    surprise_pick = next(
        (
            item
            for item in scored
            if item is not best
            and item is not value_pick
            and not _is_standalone_beverage(item.candidate)
        ),
        None,
    )
    if surprise_pick is None:
        surprise_pick = next(
            (item for item in scored if item is not best and item is not value_pick), None
        )
    result["惊喜"] = surprise_pick

    return result


def detect_conflict(intent: DiningIntent, filter_result: FilterResult) -> str | None:
    """识别条件冲突，提示用户该放宽哪一项。"""
    if filter_result.passed:
        return None

    budget = effective_budget(intent)
    parts: list[str] = []

    if budget is not None and intent.exclusions:
        parts.append(f"预算 ¥{budget.yuan:.2f} 与「不想要{'、'.join(intent.exclusions)}」的条件叠加后")
    elif budget is not None:
        parts.append(f"预算 ¥{budget.yuan:.2f} 这家门店暂时")
    elif intent.exclusions:
        parts.append(f"排除「{'、'.join(intent.exclusions)}」后这家门店暂时")
    else:
        parts.append("当前条件下这家门店暂时")

    return (
        f"{parts}没有可用选项。"
        f"{'已淘汰：' + filter_result.rejection_summary if filter_result.rejected else ''}"
        "要放宽哪一项？我可以按你的新条件重算。"
    )


def describe_breakdown(scored: ScoredCandidate) -> str:
    """把评分明细转成人话。"""
    items = sorted(scored.breakdown.items(), key=lambda kv: kv[1], reverse=True)
    return "、".join(f"{name} {value * 100:.0f}分" for name, value in items if value > 0)


def plan_combo(
    candidates: list[Candidate], intent: DiningIntent, max_items: int = 3
) -> tuple[list[Candidate], Money | None]:
    """为多人或大预算场景规划一个组合。

    组合必须是一顿像样的饭，不能是三杯饮料。
    因此：
    1. 先保证至少有一件正餐主食（汉堡/小食/拼盘等）作为基底；
    2. 再按预算与价格效率补充其他商品；
    3. 纯饮品组合直接判为无效组合，返回空列表。

    只有当商品价格全部确认时才返回总价；缺任何一个价格，
    总价返回 None，避免给出无法验证的金额。
    """
    budget = effective_budget(intent)
    priced = [item for item in candidates if item.has_confirmed_price()]
    if not priced:
        return [], None

    def _price(item: Candidate) -> int:
        return item.price.cents if item.price else 0

    # 组合基底必须是正餐主食，绝不能是几杯饮料凑数。
    # 同时排除与健康诉求冲突的甜品（如低蛋白用户收到冰淇淋）。
    avoid_dessert = any(pref in _AVOID_FOR_HEALTH_PREF for pref in intent.preferences)

    def _allowed(item: Candidate) -> bool:
        if _is_standalone_beverage(item):
            return False
        if avoid_dessert and _is_dessert(item):
            return False
        return True

    mains = sorted(
        (item for item in priced if _allowed(item)),
        key=lambda item: (_price(item), item.product_code),
    )

    base = next(
        (item for item in mains if budget is None or _price(item) <= budget.cents),
        None,
    )
    if base is None:
        return [], None

    chosen = [base]
    total = _price(base)

    target_items = intent.party_size if intent.party_size > 1 else 1
    limit = max(1, min(max_items, target_items * 2))

    # 补充项：先正餐后饮品，保证组合是一顿饭而不是一堆饮料
    supplements = sorted(
        (item for item in priced if item is not base and (avoid_dessert and _is_dessert(item) is False or not avoid_dessert)),
        key=lambda item: (_is_standalone_beverage(item), _price(item), item.product_code),
    )

    for candidate in supplements:
        if len(chosen) >= limit:
            break
        price = _price(candidate)
        if budget is not None and total + price > budget.cents:
            continue
        # 饮品最多占组合的一半，且基底已是正餐，这里允许补 1 杯饮料
        beverage_count = sum(1 for item in chosen if _is_standalone_beverage(item))
        if _is_standalone_beverage(candidate) and beverage_count * 2 >= len(chosen):
            continue
        chosen.append(candidate)
        total += price

    chosen.sort(key=lambda item: (_price(item), item.product_code))

    if any(not item.has_confirmed_price() for item in chosen):
        return chosen, None
    return chosen, Money(total)