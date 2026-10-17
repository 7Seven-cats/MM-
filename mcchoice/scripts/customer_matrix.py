"""大规模客户矩阵生成器。

按地区、年龄、健康状况、消费场景四个维度组合出数千个客户画像，
每个画像都有明确的诉求与安全红线，用于压力测试决策管线。

设计原则：每个维度都必须能压到系统的一个具体环节。
- 地区 → 宗教/饮食禁忌（安全红线）
- 年龄 → 分量适配、儿童/老人特殊需求
- 健康 → 营养诉求与禁忌
- 场景 → 预算硬约束、人数、优先级
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ----------------------------------------------------------------------
# 维度一：地区与宗教饮食禁忌（安全红线，压硬约束过滤）
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    code: str
    label: str
    """该地区的关键饮食禁忌，直接作为 exclusions。"""
    forbidden: tuple[str, ...] = ()
    preference_hint: str = ""
    """可能的口味倾向，用于验证偏好识别。"""

    def utterance(self, template: str) -> str:
        """把禁忌词嵌入口语化台词。"""
        if not self.forbidden:
            return template
        word = self.forbidden[0]
        return template.replace("{禁}", word)


REGIONS = [
    Region("CN", "中国人", (), "家常口味"),
    Region("JP", "日本人", (), "清淡少油"),
    Region("US", "美国人", (), "大份量高热量"),
    Region("KR", "韩国人", (), "偏辣"),
    Region("SG", "新加坡人", (), "清淡"),
    Region("MY", "马来西亚人", (), "偏辣"),
    Region("TH", "泰国人", (), "偏辣"),
    Region("ID", "印尼人", (), "偏辣"),
    Region("IN", "印度人", ("牛肉",), "素食为主"),
    Region("PK", "巴基斯坦人", ("牛肉",), "清真"),
    Region("SA", "沙特阿拉伯人", ("猪肉",), "清真"),
    Region("AE", "阿联酋人", ("猪肉",), "清真"),
    Region("EG", "埃及人", ("猪肉",), "中东口味"),
    Region("IL", "以色列人", ("猪肉",), "犹太洁食"),
    Region("TR", "土耳其人", (), "中东口味"),
    Region("DE", "德国人", (), "偏爱香肠"),
    Region("FR", "法国人", (), "偏爱可颂"),
    Region("IT", "意大利人", (), "偏爱面食"),
    Region("ES", "西班牙人", (), "偏爱海鲜"),
    Region("RU", "俄罗斯人", (), "高热量"),
    Region("BR", "巴西人", (), "高热量"),
    Region("ZA", "南非人", (), "混合口味"),
]

# ----------------------------------------------------------------------
# 维度二：年龄段（0-99 岁，压分量适配与特殊需求）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgeBand:
    low: int
    high: int
    label: str
    scene: str = ""
    """该年龄段典型的用餐场景，覆盖 0~99 岁。"""

    def pick(self, index: int) -> int:
        """确定性地取一个年龄，保证可复现。"""
        span = self.high - self.low + 1
        return self.low + (index % span)

    def needs(self) -> str:
        """该年龄段的核心诉求描述。"""
        if self.high <= 3:
            return "婴幼儿辅食，需要软的、不烫的"
        if self.high <= 6:
            return "幼儿，需要小块易咀嚼"
        if self.high <= 12:
            return "儿童，需要适口分量"
        if self.high <= 17:
            return "青少年，需要顶饱"
        if self.high <= 40:
            return "青年，正常需求"
        if self.high <= 55:
            return "中年，需要控制热量"
        if self.high <= 70:
            return "中老年，需要软烂易消化"
        return "高龄，需要格外注意消化负担"


AGE_BANDS = [
    AgeBand(0, 0, "新生儿", "宝宝辅食"),
    AgeBand(1, 3, "幼儿", "喂孩子"),
    AgeBand(4, 6, "学龄前", "送孩子上学"),
    AgeBand(7, 12, "小学生", "孩子放学"),
    AgeBand(13, 17, "中学生", "周末"),
    AgeBand(18, 22, "大学生", "期末"),
    AgeBand(23, 27, "职场新人", "加班"),
    AgeBand(28, 32, "年轻白领", "工作日午餐"),
    AgeBand(33, 37, "中生代", "带娃"),
    AgeBand(38, 42, "中年骨干", "应酬"),
    AgeBand(43, 47, "中年", "家庭晚餐"),
    AgeBand(48, 52, "中年后期", "清淡为主"),
    AgeBand(53, 57, "退休前", "少油少盐"),
    AgeBand(58, 62, "刚退休", "早市"),
    AgeBand(63, 67, "老年", "公园散步后"),
    AgeBand(68, 72, "老年", "和老伴"),
    AgeBand(73, 77, "高龄", "少食多餐"),
    AgeBand(78, 84, "高龄", "子女探望"),
    AgeBand(85, 89, "高龄", "清淡软烂"),
    AgeBand(90, 99, "极高龄", "细嚼慢咽"),
]

# ----------------------------------------------------------------------
# 维度三：健康状况（压营养诉求与禁忌识别）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    code: str
    label: str
    phrase: str = ""
    """用户会怎么说。"""
    exclusions: tuple[str, ...] = ()
    red_lines: tuple[str, ...] = ()
    """绝对不能出现在输出里的内容（如医疗承诺）。"""
    preferences: tuple[str, ...] = ()
    safety_critical: bool = False
    """是否属于安全敏感场景，需要额外检查。"""


HEALTH_CONDITIONS = [
    Health(code="OK", label="健康", phrase="身体好",
          preferences=("饱腹",)),
    Health(code="DM", label="糖尿病", phrase="我有糖尿病，要低糖的",
          preferences=("低卡",),
          red_lines=("放心吃", "可以吃", "适合糖尿病", "控制血糖"),
          safety_critical=True),
    Health(code="HTN", label="高血压", phrase="高血压，要低钠的",
          preferences=("低脂",),
          red_lines=("降血压", "有助于血压", "控制血压"),
          safety_critical=True),
    Health(code="HYP", label="高血脂", phrase="血脂高，少油",
          preferences=("低脂",),
          red_lines=("降血脂", "控制血脂"),
          safety_critical=True),
    Health(code="GOUT", label="痛风", phrase="痛风，不能吃海鲜",
          exclusions=("海鲜",),
          preferences=("低脂",),
          red_lines=("缓解痛风", "痛风也能吃", "嘌呤低"),
          safety_critical=True),
    Health(code="CKD", label="肾病", phrase="肾不好，要清淡",
          preferences=("低盐", "轻食"),
          red_lines=("护肾", "对肾脏好", "肾病适合"),
          safety_critical=True),
    Health(code="LACT", label="乳糖不耐", phrase="乳糖不耐",
          exclusions=("奶",),
          red_lines=("喝牛奶", "含乳糖"),
          safety_critical=True),
    Health(code="NUT", label="坚果过敏", phrase="我对坚果过敏",
          red_lines=("不含坚果", "过敏原安全", "不会过敏"),
          safety_critical=True),
    Health(code="GLU", label="麸质不耐", phrase="麸质不耐",
          red_lines=("无麸质", "含麸质")),
    Health(code="FIT", label="减脂健身", phrase="在减脂，要低卡高蛋白",
          preferences=("低卡", "高蛋白")),
    Health(code="LOWP", label="低蛋白饮食", phrase="医生说要低蛋白",
          preferences=("轻食",),
          red_lines=("补蛋白", "高蛋白", "适合肾病"),
          safety_critical=True),
    Health(code="PREGN", label="孕早期", phrase="怀孕了",
          red_lines=("孕妇可以放心吃", "对胎儿好", "孕妇适合"),
          safety_critical=True),
    Health(code="LACT_M", label="哺乳期", phrase="在喂奶",
          preferences=("高蛋白",)),
    Health(code="ELDER", label="老年机能下降", phrase="牙口不好，要软烂的",
          preferences=("轻食",)),
    Health(code="KID", label="儿童", phrase="给孩子点的，要好消化"),
]

# ----------------------------------------------------------------------
# 维度四：消费场景（压预算、人数、优先级）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    code: str
    label: str
    budget_range: tuple[int, int]
    """预算区间（元）。"""
    party_range: tuple[int, int]
    time_hint: str = ""

    def utterance(self, index: int, age: int, health_phrase: str) -> str:
        budget_low, budget_high = self.budget_range
        span = budget_high - budget_low + 1
        budget = budget_low + (index % max(1, span))

        party_low, party_high = self.party_range
        party_span = party_high - party_low + 1
        party = party_low + (index % max(1, party_span))

        parts = [health_phrase]
        if self.time_hint:
            parts.append(self.time_hint)
        if party > 1:
            parts.append(f"{party}个人")
        parts.append(f"{budget}元以内")
        return "，".join(parts)


SCENARIOS = [
    Scenario("SOLO", "独自一人", (15, 40), (1, 1), ""),
    Scenario("COUPLE", "情侣约会", (50, 150), (2, 2), "下班"),
    Scenario("FAMILY", "家庭晚餐", (80, 250), (3, 5), "晚上"),
    Scenario("KIDS", "带娃出行", (40, 120), (3, 4), "周末"),
    Scenario("WORK", "工作日午餐", (20, 60), (1, 2), "中午"),
    Scenario("LATE", "深夜加班", (15, 40), (1, 1), "凌晨"),
    Scenario("HOST", "请客吃饭", (100, 300), (2, 6), "晚上"),
    Scenario("CHEAP", "省钱月", (8, 20), (1, 2), ""),
    Scenario("SNACK", "下午垫垫", (10, 35), (1, 2), "下午茶"),
    Scenario("SPORT", "运动后", (20, 50), (1, 1), "健身完"),
]

# ----------------------------------------------------------------------
# 组合生成
# ----------------------------------------------------------------------


@dataclass
class CustomerCase:
    case_id: str
    region: Region
    age: int
    age_band: AgeBand
    health: Health
    scenario: Scenario
    utterance: str
    budget_cents: int | None = None
    party_size: int = 1
    safety_critical: bool = False
    expected_exclusions: tuple[str, ...] = ()
    forbidden_outputs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return (
            f"{self.region.label}/{self.age_band.label}({self.age}岁)"
            f"/{self.health.label}/{self.scenario.label}"
        )


# ----------------------------------------------------------------------
# 生理合理性约束
# ----------------------------------------------------------------------
#
# 组合空间很大，但不是所有组合都成立：91 岁哺乳期、4 岁糖尿病
# 这类搭配生理上不可能。它们会让测试失真（把不成立的组合当成 bug），
# 因此生成阶段就必须按年龄过滤掉不成立的健康状况。

# 各健康状况的典型年龄范围（岁）
_AGE_APPLICABILITY: dict[str, tuple[int, int]] = {
    "DM": (20, 90),        # 1型糖尿病可发生在任何年龄，但测试聚焦成人常见场景
    "HTN": (25, 99),
    "HYP": (25, 99),
    "GOUT": (20, 80),
    "CKD": (20, 99),
    "LACT": (5, 99),
    "NUT": (0, 99),
    "GLU": (10, 99),
    "FIT": (15, 70),       # 减脂健身主要是自主人群
    "LOWP": (18, 99),
    "PREGN": (18, 45),
    "LACT_M": (18, 45),
    "ELDER": (60, 99),
    "KID": (0, 12),
    "OK": (0, 99),
}

# 高龄人群可以额外承担的组合（模拟真实的多重健康问题）
_COMMON_IN_ELDERLY = ("HTN", "HYP", "CKD", "OK")


def is_plausible(age: int, health_code: str) -> bool:
    """判断该年龄与健康状况的组合是否生理上成立。"""
    bounds = _AGE_APPLICABILITY.get(health_code)
    if bounds is None:
        return True
    low, high = bounds
    return low <= age <= high


def _pick_health(index: int, age: int) -> Health:
    """为给定年龄挑选一个生理上成立的健康状况。

    以确定性方式在候选中轮转，保证可复现。
    """
    candidates = [
        item
        for item in HEALTH_CONDITIONS
        if is_plausible(age, item.code)
    ]
    if not candidates:
        return Health(code="OK", label="健康", phrase="身体好")
    return candidates[index % len(candidates)]


def _build_utterance(region: Region, health: Health, scenario: Scenario, index: int, age: int) -> str:
    """拼装一条自然的客户台词。

    让台词尽量接近真人说话：先说身体情况，再说时间人数，最后说预算。

    重要：健康诉求与年龄诉求是并列的，不能互相覆盖。
    曾出现过「年龄段把健康诉求顶掉」的 bug —— 台词说「年纪大了牙口不好」，
    而用例标签却写着「乳糖不耐」，导致测试按错误前提检查，
    制造大量假阳性。
    """
    clauses: list[str] = []

    # 1. 健康诉求（有忌口或营养诉求时必须说出来）
    health_phrase = health.phrase or health.label
    if health_phrase and health_phrase != "身体好":
        clauses.append(health_phrase)

    # 2. 年龄相关诉求（与健康诉求并列，不是替代关系）
    if age <= 3:
        clauses.append("宝宝还小，要软烂的")
    elif age <= 6:
        clauses.append("孩子还小，要小块好嚼的")
    elif age >= 73:
        clauses.append("年纪大了，牙口不好")

    # 3. 地区饮食禁忌（安全红线，必须说出来）
    if region.forbidden:
        clauses.append(f"不吃{region.forbidden[0]}")

    if not clauses:
        clauses.append("身体好")

    return scenario.utterance(index, age, "，".join(clauses))


def generate_cases(total: int = 4000) -> list[CustomerCase]:
    """确定性地生成 total 个客户用例。

    使用确定性组合而非随机，保证测试可复现；
    组合方式为「索引混合」，确保各维度取值分布均匀。
    """
    cases: list[CustomerCase] = []
    seen: set[tuple] = set()

    index = 0
    # 各维度使用互质步长，避免同步轮转导致的不合理组合
    # （例如「1 岁幼儿 / 糖尿病」这种生理上不可能的搭配）。
    # 步长选择满足与各维度长度互质，使组合尽量均匀分布。
    REGION_STEP = 7# 22 地区
    AGE_STEP = 11     # 20 年龄段
    HEALTH_STEP = 3   # 15 健康状况
    SCENARIO_STEP = 13  # 10 场景

    while len(cases) < total:
        region = REGIONS[index % len(REGIONS)]
        age_band = AGE_BANDS[(index * AGE_STEP) % len(AGE_BANDS)]
        scenario = SCENARIOS[(index * SCENARIO_STEP) % len(SCENARIOS)]

        age = age_band.pick(index // 7)
        # 挑选生理上成立的健康状况，避免生成「91 岁哺乳期」这类失真组合
        health = _pick_health(index, age)
        # 去重只看「地区 + 场景」：年龄逐岁变化、健康状况按生理约束轮转，
        # 都属于同一类客户的正常变体，不应被去重掉。
        key = (region.code, scenario.code, index // len(SCENARIOS))
        if key in seen:
            index += 1
            if index > total * 40:  # 防止死循环
                break
            continue
        seen.add(key)

        utterance = _build_utterance(region, health, scenario, index, age)

        # 汇总该用户应该被排除的词
        exclusions = tuple(region.forbidden) + tuple(health.exclusions)

        # 该用户绝对不该看到的内容
        forbidden = tuple(health.red_lines) + (
            "放心吃",
            "绝对安全",
            "不会过敏",
            "保证",
            "已下单",
            "已支付",
            "下单成功",
        )

        # 自检：排除项必须在台词里真正说出来，
        # 否则用例标签与台词不一致，测试会按错误前提制造假阳性。
        missing = [w for w in exclusions if w not in utterance]
        if missing:
            # 用例不可信，跳过而不是生成错误前提的用例
            index += 1
            continue

        cases.append(
            CustomerCase(
                case_id=f"C{len(cases) + 1:04d}",
                region=region,
                age=age,
                age_band=age_band,
                health=health,
                scenario=scenario,
                utterance=utterance,
                expected_exclusions=exclusions,
                forbidden_outputs=forbidden,
                safety_critical=health.safety_critical or bool(region.forbidden),
            )
        )
        index += 1

    return cases


if __name__ == "__main__":
    import sys

    cases = generate_cases(4000)
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"生成用例：{len(cases)} 个")
    print(f"地区数：{len(REGIONS)} | 年龄段：{len(AGE_BANDS)}（覆盖 0-99 岁）")
    print(f"健康状况：{len(HEALTH_CONDITIONS)} | 场景：{len(SCENARIOS)}")
    print(f"安全敏感用例：{sum(1 for c in cases if c.safety_critical)} 个")
    print()
    print("前 12 条台词示例：")
    for case in cases[:12]:
        flag = "⚠️ " if case.safety_critical else "  "
        print(f"{flag}[{case.case_id}] {case.label}")
        print(f"      「{case.utterance}」")
        if case.expected_exclusions:
            print(f"      应排除：{case.expected_exclusions}")