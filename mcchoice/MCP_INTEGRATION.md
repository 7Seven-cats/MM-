# 麦当劳 MCP 集成说明

本文档记录 McChoice 实际使用的麦当劳 MCP Server、工具、调用流程与业务价值。
所有协议细节均通过 `tools/list` 与 `tools/call` 实测确认，非推测。

## Server 信息

| 项目 | 值 |
| --- | --- |
| Server 名称 | `mcd-mcp` |
| Server 版本 | `1.0.0` |
| 端点 | `https://mcp.mcd.cn` |
| 传输方式 | Streamable HTTP（POST 到根路径） |
| 协议版本 | `2024-11-05` |
| 会话管理 | 无状态（响应不返回 `mcp-session-id`） |
| 工具总数 | 35 |

配置见 `mcp-config.example.json`。Token 仅通过环境变量 `MCD_MCP_TOKEN` 注入，仓库内不含任何真实凭据。

## 使用的工具（7 个，全部为只读）

| 工具 | 用途 | 在本项目中的调用位置 |
| --- | --- | --- |
| `query-nearby-stores` | 按城市与商圈定位可点餐门店 | `service.find_stores` |
| `query-meals` | 拉取门店菜单分类与商品 code | `service.list_menu` |
| `query-meal-detail` | 获取商品真实名称与套餐结构 | `service.fetch_detail` |
| `calculate-price` | 计算真实价格（唯一价格来源） | `service.calculate_price` |
| `list-nutrition-foods` | 获取营养成分数据 | `service.nutrition_index` |
| `available-coupons` | 查询可领取的优惠券 | `service.available_coupons` |
| `query-store-coupons` | 查询指定门店当前可用券 | `service.store_coupons` |
| `now-time-info` | 获取服务器时间 | `service.current_time` |

## 明确不调用的工具（7 个交易类）

为保证本项目是纯决策辅助、绝不产生任何真实消费，`McDClient.FORBIDDEN_TOOLS`
在代码层面硬性拦截以下工具，调用会直接抛出 `unsupported` 异常：

`create-order`、`cancel-order`、`auto-bind-coupons`、`draw-lottery`、
`mall-create-order`、`party-order-create`、`delivery-create-address`

对应测试：`tests/test_mcchoice.py::TestClientSafety::test_forbidden_tools_are_blocked`
与 `::test_no_forbidden_tools_called`。

## 关键协议事实（实测）

这几条是本项目技术设计的核心依据，也是本项目区别于「菜单查询机器人」的地方。

### 1. `query-meals` 不返回名称和价格

实测返回结构：

```json
{"data": {"categories": [{"name": "人气热卖", "meals": [
  {"code": "9900016076", "tags": ["心形薯饼同款比心"]},
  {"code": "9900014838", "tags": ["全球爆款", "全天供应"]}
]}]}}
```

只有 `code` 和 `tags`。**没有名称，没有价格。**

推论：任何声称"从菜单接口直接读到价格"的实现都是编造的。
本项目的价格全部来自 `calculate-price` 的真实返回。

### 2. 价格以「分」为单位

`calculate-price` 返回 `price: 2790` 表示 ¥27.90。
`models.Money` 统一以分存储，只在输出层转换。

### 3. `calculate-price` 的字段名是 `productCode` / `quantity`

使用 `code` / `count` 会返回业务错误：

```json
{"success": false, "code": 600010, "message": "缺少商品code或者商品数量quantity不正确"}
```

此结论来自 `tools/list` 返回的 `inputSchema`，并在集成验证中复现。

### 4. `beType` 决定 `beCode` 是否必传

| beType | 场景 | orderType | beCode |
| --- | --- | --- | --- |
| 1 | 到店自取 | 1 | 不传 |
| 5 | 得来速（车道取餐） | 1 | 必传 |
| 2 | 麦乐送 | 2 | 必传 |
| 6 | 企业团餐 | 2 | 必传 |

本项目当前实现聚焦 beType=1（到店自取），已按规则处理 beCode 条件传参。

### 5. `list-nutrition-foods` 返回自定义 DSL 文本

不是标准 JSON，而是形如：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  巨无霸,null,2146,513,27,26,42,961,171
```

`models.parse_nutrition_index` 按行解析，构建名称到营养数据的索引。

### 6. `available-coupons` 返回 Markdown 文本

不是 JSON。返回形如 `- 优惠券标题：免费脆薯饼`。`service.available_coupons`
做保守文本提取，拿不到就返回空列表，不做任何承诺。

### 7. `query-store-coupons` 可能返回空数组

券与门店、渠道强绑定。实测某门店返回 `data: []`，这是正常业务结果。
本项目如实告知"该门店当前暂无可用券"，不会说成"有券可用"。

### 8. 响应体的 JSON 提取需要特殊处理

麦当劳 MCP 把结构说明与业务 JSON 一起塞进 `content[].text`，且格式不统一：

- 多数工具：说明表格 + `## Original Response` + JSON
- 部分工具（如 `query-nearby-stores`）：说明文字里含嵌套 JSON 片段，
  业务 JSON 在最外层

因此 `mcd_client._extract_json_body` 采用两步策略：优先在标记后查找，
失败则全文扫描且只接受含 `success` / `code` / `data` 的顶层对象，
并用 `JSONDecoder.raw_decode` 精确定位 JSON 结束位置。

若 JSON 提取失败且内容是 Markdown，则判定为文本型工具，原样返回由调用方处理。

## 调用流程

```
用户自然语言
    ↓
intent.parse_intent          抽取预算/人数/口味/排除/场景/优先级
    ↓
service.find_stores          query-nearby-stores → 门店
    ↓
service.list_menu           query-meals → 候选池（只有 code）
    ↓
skill._preselect            按预算与标签粗筛，限制详情查询数量
    ↓
service.quote_candidates    calculate-price × N（批量并发）→ 真实价格
    ↓
service.enrich_by_name      query-meal-detail → 真实名称、套餐结构、营养匹配
    ↓
filter.filter_candidates    硬约束：排除项、预算上限
    ↓
score_candidates            四维加权评分（40/30/20/10）
    ↓
pick_three                  稳妥 / 性价比 / 惊喜
    ↓
formatter.*                  输出，并标注数据来源与不可确认项
```

## 性能

以 24 个候选的实测为例，单次决策共发起 41 次 MCP 调用：

| 工具 | 次数 | 说明 |
| --- | --- | --- |
| `query-nearby-stores` | 1 | 定位门店 |
| `query-meals` | 1 | 拉菜单 |
| `calculate-price` | 12 | 批量并发，每批 2 个商品 |
| `query-meal-detail` | 24 | 由 `detail_limit` 控制 |
| `list-nutrition-foods` | 1 | 全流程只拉一次 |
| `now-time-info` | 1 | 服务器时间 |
| `available-coupons` | 1 | 券信息 |
| `query-store-coupons` | 1 | 门店券 |

`calculate-price` 支持多商品入参，因此采用批量并发而非逐个调用，
把 N 次往返压缩到约 N/batch_size 次。`query-meal-detail` 只对粗筛后的候选调用，
避免为必然被淘汰的商品浪费调用。

## 异常处理

| 场景 | 处理方式 |
| --- | --- |
| 连接失败 | 抛出 `transport`，输出"暂时无法核实实时数据"，绝不伪装成功 |
| 响应超时 | 抛出 `timeout`，指数退避重试，上限 2 次 |
| HTTP 429/5xx | 重试；其余 4xx 直接失败，不浪费重试 |
| 业务错误 `success=false` | 抛出 `business`，记录 code 与 message |
| JSON 提取失败 | 若为 Markdown 则作文本型处理，否则抛出 `parse` |
| 返回空列表 | 输出"没有查到"，不虚构 |
| 价格缺失 | `price_status=FAILED`，评分给中性分并标注"无法验证预算" |
| 无可用候选 | 输出条件冲突并询问放宽哪一项 |
| 工具不支持 | 输出"该能力不在已开放范围内" |

重试上限由 `ClientConfig.max_retries` 控制（默认 2），
退避为 `retry_backoff * 2^(n-1)`，不会无限循环调用。

## 业务价值

1. **把「多少钱」变成可验证的事实**。菜单接口不返回价格，
   本项目用 `calculate-price` 逐个真实试算，所有金额都可追溯到 traceId。
2. **硬约束优先于智能**。用户说不吃辣、预算 20 元，
   这些是不可协商的边界；评分只在合法候选之间排序。
3. **诚实降级**。条件冲突时说明冲突在哪、该放宽什么，
   而不是编一个看起来合理的套餐。
4. **零消费风险**。代码层面拦截全部交易类工具，只做决策辅助。