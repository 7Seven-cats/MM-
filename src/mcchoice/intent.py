"""自然语言需求识别。

从用户的一句话里抽出决策所需的结构化字段。

设计原则（来自项目要求）：只询问会显著影响推荐结果的关键问题。
用户说"随便推荐一个"时，不应该追问预算、口味、人数一长串。
因此本模块区分「显式给出的字段」与「缺失的关键字段」，
由上层决定要不要追问、追问几个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 数字与单位的匹配。中文语境下「30元」「30块」「三十元」都要能识别。
# 负号必须显式排除：用户说「-50元」时，那个 50 绝不是预算上限。
# 只用 (?<![\d.]) 不够，因为短横线不是数字也不是小数点，
# 正则会从「-50元」里匹配出「50元」，把负数预算翻成正数预算，
# 反而放宽了约束 —— 这会让硬预算彻底失效。
_CURRENCY_NUM = r"(\d+(?:\.\d+)?)"
_NOT_A_NEGATIVE = r"(?<![\d.\-−－—])"
_PRICE_PATTERN = re.compile(
    rf"(?:不超过|不高于|低于|最多|预算|控制在|大概|大约|差不多)?[\s\u3000]*"
    rf"{_NOT_A_NEGATIVE}{_CURRENCY_NUM}[\s\u3000]*(?:元|块|块钱|rmb|人民币|¥|￥)",
    re.IGNORECASE,
)
_BUDGET_KEYWORDS = ("预算", "以内", "以下", "不超过", "最多", "穷", "省钱", "月底")

# 合理的点餐预算区间（分）。低于 1 元不是点餐预算，高于 2000 元是输入错误
# 或与本项目无关的大额场景，不应参与排序。
_MIN_BUDGET_CENTS = 100
_MAX_BUDGET_CENTS = 200_000

# 合理的用餐人数上限。单次决策最多处理 20 人，超过视为误输入。
_MAX_PARTY_SIZE = 20

# 注意：「十」只在 _CHINESE_UNITS 中定义，不能同时出现在这里。
# 否则解析时会先命中 digit 分支，导致所有含「十」的数词算错
# （「十五」被解析成 5、「三十」被解析成 10）。
_CHINESE_DIGITS = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}

_TASTE_PREFERENCES = {
    "辣": "辣", "香辣": "辣", "微辣": "辣", "不辣": "不辣", "不要辣": "不辣",
    "鸡肉": "鸡肉", "鸡": "鸡肉", "牛肉": "牛肉", "牛": "牛肉", "猪": "猪肉",
    "鱼": "鱼", "虾": "海鲜", "海鲜": "海鲜",
    "素": "素食", "蔬菜": "素食", "沙拉": "素食",
    "甜": "甜", "巧克力": "巧克力", "冰淇淋": "甜品", "甜品": "甜品",
    "咖啡": "咖啡", "美式": "咖啡",
    # 健康取向。这些不是口味，而是营养诉求，
    # 但客户的表达方式和口味一样（"我想吃高蛋白的"），必须能识别，
    # 否则健身人群会被当成"没说条件"，拿到与诉求无关的推荐。
    "高蛋白": "高蛋白", "蛋白质": "高蛋白", "蛋白高": "高蛋白",
    "低脂": "低脂", "低卡": "低卡", "少油": "低脂", "轻食": "轻食",
    # 医疗饮食限制。这些是医嘱级别的约束，必须被识别为偏好，
    # 否则肾病/低蛋白饮食用户会被当成「没提要求」，
    # 拿到高蛋白高糖的推荐。
    "低蛋白": "低蛋白", "限蛋白": "低蛋白", "不能吃太油": "低脂",
    "低钠": "低钠", "少盐": "低钠", "低盐": "低钠", "清淡": "低钠",
    "控糖": "低卡", "少糖": "低卡", "糖尿病": "低卡", "无糖": "低卡",
    "高蛋白": "高蛋白", "多蛋白": "高蛋白",
    "饱腹": "饱腹", "扛饿": "饱腹", "顶饱": "饱腹",
    # 常见忌口与过敏原。补充这些词是为了让「不吃奶」「乳糖不耐」
    # 这类表达能被识别为排除项，而不是被当成没说话。
    "奶": "奶", "牛奶": "奶", "乳": "奶", "乳糖": "奶", "奶昔": "奶",
    "芝士": "奶", "吉士": "奶", "起司": "奶", "酸奶": "奶", "奶昔": "奶",
    "海鲜": "海鲜", "虾": "海鲜", "蟹": "海鲜", "贝": "海鲜", "鱼": "鱼",
    "坚果": "坚果", "花生": "坚果", "杏仁": "坚果", "核桃": "坚果", "腰果": "坚果",
    "牛肉": "牛肉", "牛": "牛肉", "牛柳": "牛肉",
    "猪肉": "猪肉", "猪": "猪肉", "猪柳": "猪肉", "培根": "猪肉", "火腿": "猪肉", "猪柳蛋": "猪肉",
    "麸质": "麸质", "面筋": "麸质",
}

# 排除标记。除了显式的「不吃」，还要覆盖「过敏」「乳糖不耐」这类
# 医疗表述 —— 用户说「我对海鲜过敏」时，海鲜必须被当作排除项处理。
_EXCLUSION_MARKERS = (
    "不吃", "不要", "不喜欢", "讨厌", "不想要", "忌口", "别点", "戒",
    "过敏", "不耐", "不能吃", "禁", "避开",
)

# 过敏与不耐受的固定搭配：只要出现这些词，其后的名词就是排除项。
_ALLERGY_CONTEXT = ("过敏", "不耐", "禁", "忌")

_SCENARIOS = {
    "早餐": "早餐", "早饭": "早餐", "早上吃": "早餐", "早上": "早餐",
    "午餐": "午餐", "午饭": "午餐", "中午吃": "午餐", "中午": "午餐",
    "晚餐": "晚餐", "晚饭": "晚餐", "晚上吃": "晚餐", "晚上": "晚餐",
    "夜宵": "夜宵", "宵夜": "夜宵", "午夜": "夜宵", "半夜": "夜宵", "夜里": "夜宵",
    "加班": "加班", "下午茶": "下午茶", "通勤": "通勤",
    "追剧": "休闲", "看电影": "休闲", "休闲": "休闲",
    "聚会": "聚会", "朋友": "聚会", "同事": "聚会", "聚餐": "聚会",
    "一个人": "独食", "独食": "独食",
}

_APPETITE = {
    "很饿": "很饿", "饿坏了": "很饿", "快饿": "很饿", "巨饿": "很饿",
    "有点饿": "微饿", "不太饿": "不饿", "不饿": "不饿",
}

_PRIORITY_KEYWORDS = {
    "price": ("省钱", "便宜", "划算", "性价比", "实惠", "省钱模式"),
    "taste": ("好吃", "美味", "口味", "尝鲜", "惊喜"),
    "portion": ("吃饱", "管饱", "量足", "大份"),
    "convenience": ("方便", "快", "马上", "立刻", "着急", "赶时间"),
}


@dataclass
class DiningIntent:
    """解析出的用餐意图。字段为 None 表示用户没有明确表达。"""

    budget_cents: int | None = None
    budget_is_strict: bool = False
    """True 表示用户用了「不超过/最多」等硬约束词。"""

    party_size: int = 1
    party_size_explicit: bool = False

    preferences: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    appetite: str | None = None
    scenario: str | None = None
    priority: str | None = None
    location: str | None = None
    wants_three_options: bool = False
    wants_single_answer: bool = False
    wants_beverage: bool = False
    """用户明确表示只想喝点什么。此时饮品不再被降权。"""

    raw_text: str = ""

    def missing_critical_fields(self) -> list[str]:
        """返回会显著影响推荐结果的缺失字段。

        只有当这些字段缺失时，继续追问才是有价值的。
        预算和人数缺失时仍可基于现有数据给出合理默认方案，
        因此不视为必须追问。
        """
        missing: list[str] = []
        if self.location is None:
            missing.append("location")
        if not self.preferences and not self.exclusions:
            missing.append("taste")
        return missing

    def is_vague(self) -> bool:
        """用户是否只是丢了一句模糊的话。"""
        return (
            self.budget_cents is None
            and not self.preferences
            and not self.exclusions
            and self.scenario is None
        )


# 中文数字的单位。万与亿在点餐场景不现实，但保留以便正确拒绝而非误算。
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_CHINESE_ALL = frozenset(_CHINESE_DIGITS) | frozenset(_CHINESE_UNITS)


def _parse_chinese_number(text: str) -> int | None:
    """把中文数字转成整数。支持到「亿」，覆盖点餐与人数场景。

    实现说明：采用「按段累加」的标准写法。
    「三十五」= 三(3) 十(10) 五(5) -> 3*10 + 5 = 35
    「一百」  = 一(1) 百(100)      -> 1*100     = 100

    旧实现把「三十五」逐位拼接成 405、「十五」拼成 105、「一百」直接返回 None，
    会让用户说的 30 元被当成 40 元，属于会算错预算的严重缺陷。
    """
    text = text.strip()
    if not text:
        return None
    if not all(char in _CHINESE_ALL for char in text):
        return None

    total = 0
    current = 0
    for char in text:
        # 先判单位：单位与数字的判定顺序必须与字典定义互斥且一致
        if char in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[char]
            if unit >= 10000:
                # 「万」「亿」是段倍率：先把本段累加进 total，再整体乘倍率。
                # 「一万」= (0 + 1) * 10000 -> 10000
                total = (total + current) * unit
                current = 0
            else:
                # 「十」「百」「千」：前面没有数字时省略为一，如「十五」= 1 * 10 + 5
                total += current * unit if current else unit
                current = 0
        else:
            current = _CHINESE_DIGITS[char]

    result = total + current
    return result if result > 0 else None


def extract_budget(text: str) -> tuple[int | None, bool]:
    """抽取预算，返回 (分, 是否硬上限)。

    「30元以内」是硬约束；「大概30元」是软偏好。
    两者在排序时权重不同，因此必须区分。
    """
    strict_markers = ("不超过", "不高于", "低于", "最多", "以内", "以下", "不能超过", "不能多于")

    # 形式零：数字后直接跟限制词，省略了单位字。
    # 「150以内」「预算150」「最多150」在口语里非常常见，
    # 之前的正则要求「数字+单位」会完全漏掉这种写法。
    bare = re.search(
        rf"{_NOT_A_NEGATIVE}{_CURRENCY_NUM}[\s\u3000]*"
        r"(?:以内|以下|之内|内|封顶)",
        text,
    )
    if bare:
        cents = int(float(bare.group(1)) * 100)
        if _MIN_BUDGET_CENTS <= cents <= _MAX_BUDGET_CENTS:
            return cents, True
    bare_prefix = re.search(
        rf"(?:预算|最多|不超过|不高于|控制在|封顶)[\s\u3000]*"
        rf"{_NOT_A_NEGATIVE}{_CURRENCY_NUM}(?![\d.])",
        text,
    )
    if bare_prefix:
        cents = int(float(bare_prefix.group(1)) * 100)
        if _MIN_BUDGET_CENTS <= cents <= _MAX_BUDGET_CENTS:
            return cents, True

    # 形式一：数字在前（30元 / 30块 / 30块以内）
    for match in _PRICE_PATTERN.finditer(text):
        window = text[max(0, match.start() - 6) : match.end()]
        amount = float(match.group(1))
        # 「三十元」这类中文数字走单独分支
        chinese = re.search(r"([零一二两三四五六七八九十百千万亿]+)\s*(?:元|块)", text)
        if chinese and not match.group(1):
            parsed = _parse_chinese_number(chinese.group(1))
            if parsed is not None:
                amount = parsed
        is_strict = any(marker in window for marker in strict_markers) or any(
            marker in text for marker in _BUDGET_KEYWORDS
        )
        cents = int(amount * 100)
        # 只接受合理的点餐预算区间，避免「999999元」这类异常值把排序搅乱
        if not _MIN_BUDGET_CENTS <= cents <= _MAX_BUDGET_CENTS:
            continue
        return cents, is_strict

    # 形式二：货币符号在前（¥30 / ￥30）。前面的正则要求单位在后，
    # 因此这里单独处理，避免用户打「¥30」时预算完全被忽略。
    symbol = re.search(rf"[¥￥]\s*{_NOT_A_NEGATIVE}{_CURRENCY_NUM}", text)
    if symbol:
        cents = int(float(symbol.group(1)) * 100)
        if _MIN_BUDGET_CENTS <= cents <= _MAX_BUDGET_CENTS:
            return cents, any(marker in text for marker in _BUDGET_KEYWORDS)

    # 形式三：纯中文数字（三十元 / 十五块 / 一百块）
    chinese = re.search(r"([零一二两三四五六七八九十百千万亿]+)[\s\u3000]*(?:元|块)", text)
    if chinese:
        parsed = _parse_chinese_number(chinese.group(1))
        if parsed is not None:
            cents = parsed * 100
            if _MIN_BUDGET_CENTS <= cents <= _MAX_BUDGET_CENTS:
                # 「三十元以内」是硬约束；单说「三十元」是软偏好
                is_strict = any(marker in text for marker in _BUDGET_KEYWORDS) or any(
                    marker in text for marker in strict_markers
                )
                return cents, is_strict
    return None, False


# 中文数量词与「人」的组合。带娃家庭极其常见，漏掉会导致人数严重偏小。
# 「两大一小」= 3 人、「一大家子」= 家庭聚餐、「三口人」= 3 人。
_CHINESE_COUNT = "零一二两三四五六七八九十百千万亿"

# 家庭聚餐的人数估计。无法从文本算出确切人数时，
# 按「一顿饭至少这些人」来准备，宁可多算也不要少算。
_FAMILY_PARTY_ESTIMATE = 4


def _party_from_two_big_one_small(text: str) -> int | None:
    """解析「两大一小」这类组合表达，返回总人数。

    「两大一小」= 2 个大人 + 1 个小孩 = 3 人。
    这个表达在带娃出行的场景里极常见，漏掉会把 3 人算成 1 人。
    """
    match = re.search(
        rf"(\d+|[{_CHINESE_COUNT}]+)\s*大\s*(?:\d+|[{_CHINESE_COUNT}]+)\s*小", text
    )
    if not match:
        return None

    def _to_int(token: str) -> int | None:
        token = token.strip()
        if token.isdigit():
            return int(token)
        return _parse_chinese_number(token)

    adults = _to_int(match.group(1))
    kids_text = re.search(rf"(\d+|[{_CHINESE_COUNT}]+)\s*小", match.group(0))
    kids = _to_int(kids_text.group(1)) if kids_text else 1
    if adults is None or kids is None:
        return None
    total = adults + kids
    return total if 1 <= total <= _MAX_PARTY_SIZE else None


def extract_party_size(text: str) -> tuple[int, bool]:
    """抽取用餐人数。"""
    # 先处理组合表达（两大一小），它的优先级高于单词数
    combined = _party_from_two_big_one_small(text)
    if combined is not None:
        return combined, True

    patterns = [
        (re.compile(r"(\d+)\s*(?:个?人|位|份)"), None),
        (re.compile(rf"([{_CHINESE_COUNT}]+)\s*个?人"), None),
    ]
    for pattern, _ in patterns:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            size = int(raw) if raw.isdigit() else _parse_chinese_number(raw)
            if size and 1 <= size <= _MAX_PARTY_SIZE:
                return size, True
    # 「三口人」「五口」——中文口语里按「口」计人数
    mouth = re.search(rf"(\d+|[{_CHINESE_COUNT}]+)\s*口(?:人)?", text)
    if mouth:
        raw = mouth.group(1)
        size = int(raw) if raw.isdigit() else _parse_chinese_number(raw)
        if size and 1 <= size <= _MAX_PARTY_SIZE:
            return size, True

    # 「两个大人一个小孩」——把「大」「小」「孩子」等称谓逐个数出来
    roles = re.findall(r"(?:大人|小孩|孩子|大人|娃|人)", text)
    if len(roles) >= 2:
        return min(len(roles), _MAX_PARTY_SIZE), True

    # 「一大家子」「全家」「家里吃饭」——人数不明，按家庭聚餐估计。
    # 注意「一大家子」里的大家不是「大人」，因此必须靠这些词单独识别。
    if any(marker in text for marker in ("一家", "大家", "全家", "家里", "家庭", "几口")):
        return _FAMILY_PARTY_ESTIMATE, True

    if "一个人" in text or "单人" in text:
        return 1, True
    if "两个人" in text or "二人" in text:
        return 2, True
    return 1, False


def extract_exclusions(text: str) -> list[str]:
    """抽取用户明确排除的食物。硬约束，优先级最高。

    覆盖三类表达：
    1. 显式否定：「不吃辣」「不要香菜」「别给我牛肉」
    2. 过敏与不耐受：「对海鲜过敏」「乳糖不耐」「坚果过敏」
    3. 间接否定：「痛风，不能吃海鲜」「痛风不能碰海鲜」

    第2、3 类是实测发现缺失的：原实现只认「标记后紧跟关键词」，
    因此「痛风，不能吃海鲜」里的海鲜会被漏掉，
    导致痛风用户被推荐海鲜类商品 —— 这属于安全问题。
    """
    exclusions: list[str] = []

    def _collect_keywords(fragment: str) -> None:
        for keyword, label in _TASTE_PREFERENCES.items():
            if keyword in fragment:
                exclusions.append(label)

    # --- 情况一：显式否定标记 ---
    for marker in _EXCLUSION_MARKERS:
        for match in re.finditer(re.escape(marker) + r"([^，。,.;；\n]{0,8})", text):
            _collect_keywords(match.group(1))

    # --- 情况二：过敏 / 不耐受 / 忌口类医疗表述 ---
    # 「对X过敏」「X过敏」「乳糖不耐」「X不耐受」
    # 这里不看标记位置，只要出现过敏/不耐字样，
    # 就在整个句子里找可识别的食材词。
    if any(context in text for context in _ALLERGY_CONTEXT):
        # 去掉过敏相关的字样，避免把「过敏」本身当成食材
        residue = text
        for context in _ALLERGY_CONTEXT:
            residue = residue.replace(context, "")
        for phrase in ("对", "受", "的", "了", "有", "是", "不能", "不能吃", "碰"):
            residue = residue.replace(phrase, "")
        _collect_keywords(residue)

    # --- 情况三：逗号分隔的间接否定 ---
    # 「痛风，不能吃海鲜」：逗号前是疾病名，逗号后是「不能吃X」
    for segment in re.split(r"[，,；;]", text):
        if any(negation in segment for negation in ("不能吃", "不能碰", "吃不了", "别吃", "忌")):
            _collect_keywords(segment)

    # 特别处理否定形式的辣
    if any(marker in text for marker in ("不辣", "不要辣", "别辣")) and "辣" not in exclusions:
        exclusions.append("辣")

    # 排除项应按「具体」排序：海鲜优于鱼，奶优于奶昔的泛化标签
    return list(dict.fromkeys(exclusions))


def extract_preferences(text: str) -> list[str]:
    """抽取口味偏好。"""
    preferences: list[str] = []
    for keyword, label in _TASTE_PREFERENCES.items():
        if keyword in text:
            # 否定表达归到排除，不算偏好
            if any(marker + keyword in text for marker in ("不", "讨厌", "不要")):
                continue
            preferences.append(label)
    return list(dict.fromkeys(preferences))


def parse_intent(text: str, location: str | None = None) -> DiningIntent:
    """把一句自然语言解析成 DiningIntent。"""
    text = (text or "").strip()

    budget, strict = extract_budget(text)
    party_size, party_explicit = extract_party_size(text)

    preferences = extract_preferences(text)
    exclusions = extract_exclusions(text)
    # 排除项不应同时留在偏好里
    preferences = [item for item in preferences if item not in exclusions]

    appetite = next((label for keyword, label in _APPETITE.items() if keyword in text), None)
    scenario = next((label for keyword, label in _SCENARIOS.items() if keyword in text), None)

    priority = None
    for level, keywords in _PRIORITY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            priority = level
            break
    if priority is None and any(word in text for word in ("随便", "都行", "无所谓", "你决定")):
        priority = "taste"

    wants_three = any(word in text for word in ("三个", "3个", "三选", "三个方案", "三个选择"))
    wants_single = any(
        word in text for word in ("直接决定", "别列", "不要列", "一个就行", "别给我列", "快点决定")
    )
    wants_beverage = any(
        word in text for word in ("喝点", "喝的", "饮料", "咖啡", "美式", "奶茶", "果汁", "来杯")
    )

    detected_location = location
    if detected_location is None:
        location_match = re.search(r"(?:在|去|回)\s*([一-龥]{2,8}?)(?:附近|这边|那儿|吃|点)", text)
        if location_match:
            detected_location = location_match.group(1)
        else:
            city_match = re.search(r"([一-龥]{2,6}?)(?:的)?麦当劳", text)
            if city_match:
                detected_location = city_match.group(1)

    return DiningIntent(
        budget_cents=budget,
        budget_is_strict=strict,
        party_size=party_size,
        party_size_explicit=party_explicit,
        preferences=preferences,
        exclusions=exclusions,
        appetite=appetite,
        scenario=scenario,
        priority=priority,
        location=detected_location,
        wants_three_options=wants_three and not wants_single,
        wants_single_answer=wants_single,
        wants_beverage=wants_beverage,
        raw_text=text,
    )


def build_followup_questions(intent: DiningIntent) -> list[str]:
    """生成值得追问的问题。模糊请求时返回空列表，直接给答案。"""
    if intent.is_vague() or intent.wants_single_answer:
        return []
    questions: list[str] = []
    missing = intent.missing_critical_fields()
    if "location" in missing and not intent.location:
        questions.append("你在哪个城市或商圈？我按附近门店的真实菜单来推荐。")
    if "taste" in missing:
        questions.append("有没有特别想吃或者不想吃的？比如辣、鸡肉、甜品。")
    return questions