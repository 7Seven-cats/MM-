# 参赛声明

## 一、原创性声明

本项目 McChoice（麦麦选择困难症终结者）由参赛者独立设计、开发完成。

- 项目名称、英文名称、核心定位、业务流程、推荐算法、交互文案均为原创设计。
- 项目**未使用其他参赛者的代码、文档或提示词**，未复制任何已有作品。
- 项目使用的第三方依赖仅为 Python 标准库（`urllib`、`json`、`concurrent.futures`、
  `dataclasses`、`threading` 等），未引入任何第三方运行时依赖。
  测试框架为可选项：安装了 pytest 可用 pytest，未安装时使用自带的零依赖运行器。
- 项目内所有代码注释与文档均为原创撰写。

## 二、真实使用麦当劳 MCP 的声明

本项目**真实调用**麦当劳 MCP（`https://mcp.mcd.cn`），并在开发过程中完成了完整的
集成验证。所有协议细节（工具清单、字段名、金额单位、beCode 传参规则、
响应体格式）均通过 `tools/list` 与 `tools/call` 实测确认，而非依据文档推测或臆造。

具体的实测结论已完整记录在 `MCP_INTEGRATION.md` 中，包括：

- Server 版本、协议版本、传输方式、无状态会话管理
- 服务端开放的 35 个工具清单及本项目实际使用的 7 个只读工具
- `query-meals` 不返回名称与价格的实测返回样例
- `calculate-price` 的 `productCode` / `quantity` 字段规范与错误码 600010
- 金额以「分」为单位的实测样例
- `list-nutrition-foods` 自定义 DSL 格式的真实返回样例
- `query-store-coupons` 返回空数组的真实业务结果

复现方式：

```bash
export MCD_MCP_TOKEN="<你的 token>"
python scripts/integration_check.py --city 南京 --keyword 新街口 --query "30元以内随便吃"
```

该脚本会打印每一步真实 MCP 调用的工具名、耗时与服务端 traceId，
并将审计记录导出为 JSON 作为调用凭证。

## 三、合规性声明

### 3.1 只读，不产生任何真实消费

本项目在**代码层面**硬性拦截全部交易类工具。
`McDClient.FORBIDDEN_TOOLS` 包含 `create-order`、`cancel-order`、
`auto-bind-coupons`、`draw-lottery`、`mall-create-order`、`party-order-create`、
`delivery-create-address`，调用这些工具会直接抛出 `unsupported` 异常。

本项目不会、也无能力代替用户下单、支付、领取优惠券或消耗用户积分。
`query-my-coupons`、`query-store-coupons` 等仅用于**只读展示**。

对应测试用例：
- `TestClientSafety::test_forbidden_tools_are_blocked`
- `TestSkillEndToEnd::test_no_forbidden_tools_called`
- `TestSkillEndToEnd::test_coupon_lookup_is_read_only`

### 3.2 不伪造业务数据

本项目遵循「不确定即不承诺」原则：

- 价格**只**来自 `calculate-price` 的真实返回。价格未确认的候选，
  其 `price_status` 标记为 `FAILED`，评分中预算契合度归零，
  且在输出中明确标注"价格暂无法确认"。
- 组合总价仅在**所有商品价格均已确认**时才计算，否则返回 `None` 并说明原因。
- 营养数据仅在名称精确匹配时使用。**不做任何模糊推断**，
  匹配失败时保持 `None`，并输出"无营养数据，不对热量做判断"。
- 优惠券区分「可领取」与「本门店可用」两个概念。
  `query-store-coupons` 返回空数组时，如实告知"该门店当前暂无可用券"。
- MCP 调用失败时，输出中明确说明无法核实实时数据，绝不降级为编造的结果。

对应测试用例：
- `TestHardConstraints::test_missing_price_is_never_claimed_affordable`
- `TestComboPlanning::test_combo_returns_empty_when_no_priced_items`
- `TestNutrition::test_no_fuzzy_nutrition_guess`
- `TestSkillEndToEnd::test_mcp_failure_degrades_without_faking`
- `TestSkillEndToEnd::test_never_claims_order_placed`

### 3.3 数据来源合法

项目仅使用麦当劳官方 MCP 提供的公开查询能力，
仅在本项目运行期间临时调用，不存储、不转售、不二次分发任何用户业务数据。
项目的审计日志仅记录工具名、入参、耗时、traceId 与成败状态，
**不记录** Token、用户身份信息或支付信息。

## 四、敏感信息声明

本项目仓库中**不包含**任何以下内容：

- 麦当劳 MCP Access Token 或任何真实密钥
- 用户的手机号、地址、订单号、会员信息
- 任何真实用户的个人信息
- 任何真实订单数据

具体措施：

- Token 仅通过环境变量 `MCD_MCP_TOKEN` 注入，代码中无任何硬编码凭据。
- `mcp-config.example.json` 仅含环境变量占位符 `${MCD_MCP_TOKEN}`。
- 客户端配置缺失 Token 时抛出异常，异常信息中**不回显** Token 内容，
  避免凭据进入日志。
- 仓库已配置 `.gitignore`，排除 `.env`、审计日志导出文件等。

## 五、测试数据声明

本项目测试使用 `tests/fake_client.py` 中的**人工构造夹具**。

- 这些夹具中的门店名、地址、商品名、价格**全部为虚构**，
  例如「测试汉堡套餐A」「测试门店（非真实数据）」。
- 夹具文件顶部与类文档字符串均明确标注其为测试数据。
- 夹具**不会**出现在面向用户的输出中，也**不用于**任何演示场景。
- 真实业务数据仅通过 `scripts/integration_check.py` 在运行时获取，
  不写入仓库、不进入测试断言。
- 两类数据物理隔离：单元测试不访问网络，集成验证脚本独立于测试套件。

## 六、不做医疗与健康建议

本项目展示的营养数据（如热量、蛋白质含量）**仅来自麦当劳 MCP 官方接口的原样返回**，
仅作为商品信息的补充说明，不构成任何营养建议、减脂建议或健康指导。

用户提及食物过敏时，本项目**不做安全确认**，只会提示核实官方配料及过敏原信息。
普通文本匹配不等于安全确认，本项目不会因为商品名称里没有「花生」二字
就声称该商品不含花生。

## 七、原创许可

本项目以 MIT 许可证开源，许可证全文见 `LICENSE`。