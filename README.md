# McChoice · 麦麦选择困难症终结者

> 「我饿了，30 元以内吃什么？」——别再打开菜单App 翻八分钟了。

基于**麦当劳 MCP** 的点餐决策 Skill。它不猜、不编、不假装，
所有价格和商品信息都来自麦当劳 MCP 的真实调用。

---

## 这个项目和普通菜单查询机器人有什么不一样

麦当劳 MCP 的 `query-meals` 接口**只返回商品编码和标签，不返回名称，也不返回价格**。

```
{"code": "9900014838", "tags": ["全球爆款", "全天供应"]}
```

这意味着：如果你看到某个点餐助手信誓旦旦地说「巨无霸 ¥26.5，推荐给你」，
它要么是记下来的旧数据，要么是编的。

McChoice 的每一个价格，都来自 `calculate-price` 的实时试算。
**没有算过的价格，我们就说「价格暂无法确认」。**

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 自然语言需求识别 | 从一句话里抽出预算、人数、口味、排除项、场景、优先级 |
| 真实数据获取 | 定位门店 → 拉菜单 → 批量算价 → 补名称与营养 |
| 三选一推荐 | 稳妥之选 / 性价比之选 / 惊喜之选，每档都有不同理由 |
| 预算挑战模式 | 硬预算绝不被突破，放不下时如实说明而非降级凑数 |
| 硬约束优先 | 排除项永不进推荐，无论评分多高 |
| 只读零风险 | 代码层面拦截全部交易类工具，不下单不领券不抽奖 |
| 诚实降级 | MCP 挂了就明说，不拿旧数据冒充实时数据 |

## 快速开始

### 环境要求

- Python 3.10+
- 麦当劳 MCP Access Token（从官方渠道获取）

### 安装

```bash
git clone <your-repo-url>
cd mcchoice

# 项目本身零第三方运行时依赖
python -c "import mcchoice"  # 验证
```

### 配置

```bash
export MCD_MCP_TOKEN="<你的 token>"
```

Token 只从环境变量读取，代码里没有任何硬编码凭据。

### 在 Agent 中使用

`skill/SKILL.md` 是标准的 Agent Skill 定义文件，可被 WorkBuddy 等平台加载。
其核心逻辑由 `src/mcchoice/` 提供：

```python
from mcchoice.skill import McChoiceSkill

skill = McChoiceSkill.from_env()
result = skill.decide("30元以内随便吃", city="南京", keyword="新街口")
print(result.answer)
```

### 真实集成验证

```bash
export MCD_MCP_TOKEN="<你的 token>"
python scripts/integration_check.py --city 南京 --keyword 新街口 --query "30元以内随便吃"
```

该脚本会打印每一步真实 MCP 调用的工具名、耗时与服务端 `traceId`。

## 真实运行效果

以下是真实调用麦当劳 MCP 的输出（2026-10-17，南京新街口）：

**输入：** `30元以内随便吃`

```
### 麦麦已经替你决定了

**今天首选：巨无霸**

- 预计总价：¥26.50
- 推荐理由：预算契合 30分、需求匹配 24分、口味匹配 12分、场景适配 6分
- 适合场景：麦当劳南京新百餐厅
- 数据依据：价格来自麦当劳 MCP calculate-price 实时计算，查询时间：2026-10-17 16:58:14

**如果你想换个口味，还有备选：**

1. **性价比之选**：如意百宝袋
   - 价格：¥12.00
   - 为什么选它：比首选便宜 ¥14.50
   - 注意：无营养数据，不对热量做判断；价格明显低于预算
2. **惊喜之选**：培根蔬萃双层牛堡
   - 价格：¥25.00

选择困难 cured，下一步是找座位。

### 券的情况

麦麦省当前可领的券：
- 巧克力味厚松饼猪柳蛋套餐
- 免费脆薯饼
- 人气麦旋风买一送一
- 9.9元中杯冰美式

该门店当前暂无可用券，这是正常的 —— 券是门店和渠道强绑定的。
```

单次决策共发起 **41 次真实 MCP 调用**，候选池 24 个商品。

## 工作原理

```
用户自然语言
    ↓
意图解析          预算 / 人数 / 口味 / 排除项 / 场景 / 优先级
    ↓
query-nearby-stores        定位门店
    ↓
query-meals                候选池（只有 code，没有价格）
    ↓
calculate-price × N        真实价格（批量并发）
    ↓
query-meal-detail          真实名称、套餐结构
    ↓
硬约束过滤          排除项、预算上限
    ↓
四维加权评分        需求 40% / 预算 30% / 口味 20% / 场景 10%
    ↓
三选一输出          稳妥 / 性价比 / 惊喜
```

## 评分权重

初始权重是设计参数，不是客观事实，集中在 `engines/recommender.py::DEFAULT_WEIGHTS`：

| 维度 | 权重 |
| --- | --- |
| 需求匹配 | 40% |
| 预算契合 | 30% |
| 口味匹配 | 20% |
| 场景适配 | 10% |

**硬约束永远优先于总分**：用户明确排除的商品不进推荐，
明确的预算上限不会被突破，价格未确认的商品不会被声称为「预算内」。

## 安全边界

本项目**在代码层面**拦截全部交易类工具：

```python
FORBIDDEN_TOOLS = frozenset({
    "create-order", "cancel-order", "auto-bind-coupons", "draw-lottery",
    "mall-create-order", "party-order-create", "delivery-create-address",
})
```

调用它们会直接抛出 `unsupported` 异常。
McChoice 是决策辅助，不会也不会让你误下单、误扣积分。

## 测试

```bash
# 零依赖运行器（无需安装 pytest）
python tests/run_tests.py

# 或使用 pytest
pip install pytest && python -m pytest tests -q
```

当前 **82 个测试全部通过**，覆盖：

- 需求解析（预算软硬约束区分、人数、口味、排除项）
- 硬约束（排除项永不推荐、预算不被突破、价格缺失不冒充便宜）
- 评分与排序稳定性（相同输入必得相同结果）
- 组合总价校验（价格不全不给总价）
- 营养数据解析（拒绝模糊匹配）
- 端到端（模糊请求、位置缺失、单答案、MCP 故障、空返回）
- 安全（禁止交易工具、不声称已下单）

测试使用 `tests/fake_client.py` 中的**人工构造夹具**，
全部商品名为「测试汉堡套餐A」这类虚构数据，不含任何真实业务数据。
测试不访问网络。

## 项目结构

```
mcchoice/
├── README.md                    本文件
├── CONTEST_DECLARATION.md       参赛声明（原创性 / 合规 / 敏感信息）
├── MCP_INTEGRATION.md           MCP 工具映射、协议实测结论、异常处理
├── mcp-config.example.json      脱敏配置示例
├── workbuddy.md                 WorkBuddy 专项奖励用对话上下文
├── LICENSE                      MIT
├── src/mcchoice/
│   ├── skill.py                 编排层：完整决策流程
│   ├── intent.py                自然语言需求识别
│   ├── models.py                领域模型、金额单位、营养解析
│   ├── formatter.py             输出格式化与降级文案
│   ├── adapters/
│   │   ├── mcd_client.py        MCP 协议客户端（唯一网络出口）
│   │   └── service.py           业务门面（只读）
│   └── engines/
│       └── recommender.py       硬约束过滤与推荐排序
├── tests/
│   ├── test_mcchoice.py         46 个核心测试
│   ├── test_edge_cases.py       36 个边界、过敏安全与医疗诉求回归测试
│   ├── fake_client.py           构造夹具（非真实数据）
│   └── run_tests.py             零依赖运行器
├── scripts/
│   ├── integration_check.py     真实 MCP 集成验证
│   ├── customer_scenarios.py    10 类真实客户画像测试
│   ├── customer_matrix.py       4000 客户矩阵生成器（22 地区 × 0-99 岁 × 15 健康状况）
│   ├── mass_test.py             4000 客户全量回放 + 抽样真实 MCP
│   └── fuzz_test.py             42 条边界输入模糊测试
└── skill/
    └── SKILL.md                 Agent Skill 定义
```

## 目标用户

- **纠结党**：下班站在店里不想打开App翻菜单的人
- **预算党**：明确知道今天只能花多少钱的人
- **效率党**：赶时间、需要一个确定答案的人
- **开发者**：想看麦当劳 MCP 真实能力边界与工程实践的人

## 已知限制

- 当前实现聚焦 `beType=1`（到店自取）。外送（2）、得来速（5）、团餐（6）
  的 `beCode` 传参规则已按官方 schema 实现，但未做完整业务验证。
- 营养数据取自 `list-nutrition-foods`（160 条常见餐品），
  并非与门店菜单一一对应，匹配不上时不做推断。
- 口味与场景匹配基于文本关键词，不涉及配料表与过敏原判断。
- 不支持下单。用户需要自行在麦当劳 APP 完成交易。

## 许可证

MIT，见 [LICENSE](LICENSE)。
本项目与麦当劳无隶属关系，仅使用其公开开放的 MCP 能力。
