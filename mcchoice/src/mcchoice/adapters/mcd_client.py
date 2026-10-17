"""麦当劳 MCP Server 的协议客户端。

本模块是整个项目与麦当劳 MCP 通信的唯一出口。所有业务模块
（需求解析、推荐引擎、输出格式化）都不得直接发起 HTTP 请求，
必须经过 McDClient，以保证：

1. 重试策略、超时控制、错误分类只在一处实现；
2. 单元测试可以用 FakeClient 替换真实网络调用；
3. 真实调用记录可以被结构化地写入审计日志。

协议事实（已通过 tools/list + tools/call 实测确认，非推测）：

- 传输方式：Streamable HTTP，POST 到服务端根路径
- 必需请求头：Authorization: Bearer <token>
- 协议版本：2024-11-05
- 会话管理：无状态（响应中不返回 mcp-session-id）
- 请求体：{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{...}}
- 响应体：result.content[].text 内含"## Original Response"分隔的 JSON
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"
USER_AGENT = "mcchoice/1.0 (McDonald's MCP Contest Project)"

# 响应正文中真实存在的分隔标记。麦当劳 MCP 会在 content[].text 里
# 先输出一段字段说明表格，再输出 "## Original Response" 和真正的 JSON。
_ORIGINAL_RESPONSE_MARKER = "## Original Response"

_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

# 复用的 JSON 解码器，raw_decode 能精确解析出一个完整 JSON 值。
_JSON_DECODER = json.JSONDecoder()


def _looks_like_business_payload(fragment: str) -> bool:
    """判断片段是否是麦当劳的业务响应体。

    业务响应体必然是含 success / code / data 字段的顶层对象。
    这个判据比"能不能解析成 JSON"更严格，可以排除说明文字里
    嵌套的 JSON 片段（如 reservationTimeOptions 的元素）。
    """
    if not _looks_like_json_object(fragment):
        return False
    stripped = fragment.lstrip()
    try:
        value, _end = _JSON_DECODER.raw_decode(stripped)
    except ValueError:
        return False
    return isinstance(value, dict) and (
        "success" in value or "code" in value or "data" in value
    )


def _extract_json_body(text: str) -> str | None:
    """从工具返回文本中提取业务 JSON。

    麦当劳 MCP 的 content[].text 结构不统一，实测存在两种形态：

    形态一（多数工具）：结构说明表格 + "## Original Response" + JSON
    形态二（部分工具，如 query-nearby-stores）：
        说明文字里含嵌套 JSON 片段，真正的业务 JSON 在最后。

    因此采取两步策略：
    1. 优先在 "## Original Response" 标记之后找业务 JSON；
    2. 找不到则全文扫描，只接受含 success/code/data 的顶层对象。

    这样既不会误抓嵌套片段，也不会因为格式差异而取不到数据。
    """
    body = text.strip()
    if not body:
        return None

    def _slice(value: str) -> str | None:
        stripped = value.lstrip()
        try:
            _decoded, end = _JSON_DECODER.raw_decode(stripped)
            return stripped[:end]
        except ValueError:
            return None

    marker_at = body.rfind(_ORIGINAL_RESPONSE_MARKER)
    if marker_at >= 0:
        tail = body[marker_at + len(_ORIGINAL_RESPONSE_MARKER) :].strip()
        if _looks_like_business_payload(tail):
            return _slice(tail)
        # 标记后可能还跟着说明文字，继续全文扫描

    for start in _iter_object_starts(body):
        candidate = body[start:]
        if _looks_like_business_payload(candidate):
            return _slice(candidate)
    return None


def _looks_like_json_object(fragment: str) -> bool:
    """用 raw_decode 判断片段是否是完整可解析的 JSON 值。

    不能简单地取到字符串结尾再 json.loads —— 因为 JSON 后面可能还跟着
    其他说明文字，且 JSON 内部有嵌套对象。raw_decode 只解析一个完整的
    JSON 值并返回结束位置，正好解决这个问题。
    """
    stripped = fragment.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        _value, _end = _JSON_DECODER.raw_decode(stripped)
        return True
    except ValueError:
        return False


def _iter_object_starts(text: str):
    """从前向后产出所有 '{' 的位置。

    顺序很重要：麦当劳 MCP 的结构说明文字里可能包含嵌套的 JSON 片段，
    真正的业务 JSON 是最后那个最外层的对象。从前往后找可以跳过前面
    的说明文字，第一个能完整解析的对象才是顶层结果。
    """
    for index, char in enumerate(text):
        if char == "{":
            yield index


class McDErrorKind:
    """错误分类。用于决定是否重试，以及如何向用户降级说明。"""

    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    RPC = "rpc"
    BUSINESS = "business"
    PARSE = "parse"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"


class McDError(Exception):
    """所有麦当劳 MCP 调用失败的统一异常类型。"""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        tool: str | None = None,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.tool = tool
        self.retryable = retryable
        self.detail = detail

    def user_facing_note(self) -> str:
        """给用户看的降级说明，绝不伪装成成功。"""
        return {
            McDErrorKind.TRANSPORT: "麦当劳服务暂时连不上，无法核实实时菜单与价格。",
            McDErrorKind.TIMEOUT: "麦当劳服务响应超时，无法核实实时菜单与价格。",
            McDErrorKind.HTTP_STATUS: "麦当劳服务返回异常状态，暂时无法核实实时数据。",
            McDErrorKind.RPC: "麦当劳 MCP 调用被拒绝，本次没有拿到可用数据。",
            McDErrorKind.BUSINESS: "麦当劳系统拒绝了本次查询（可能是参数或门店不支持）。",
            McDErrorKind.PARSE: "麦当劳返回的数据无法解析，无法确认真实内容。",
            McDErrorKind.EMPTY: "该门店当前没有返回可用数据。",
            McDErrorKind.UNSUPPORTED: "该能力不在麦当劳 MCP 已开放的工具范围内。",
        }.get(self.kind, "麦当劳数据获取失败，无法确认实时信息。")


@dataclass
class CallRecord:
    """单次真实调用的审计记录，用于写入 docs/ 里的集成验证证据。"""

    tool: str
    arguments: dict[str, Any]
    ok: bool
    latency_ms: int
    server_datetime: str | None = None
    trace_id: str | None = None
    error_kind: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "latencyMs": self.latency_ms,
            "serverDatetime": self.server_datetime,
            "traceId": self.trace_id,
            "errorKind": self.error_kind,
            "errorMessage": self.error_message,
        }


@dataclass
class ClientConfig:
    """客户端配置。

    token 必须来自环境变量 MCD_MCP_TOKEN，仓库中不保存任何真实凭据。
    """

    endpoint: str = DEFAULT_ENDPOINT
    token: str | None = None
    timeout: float = 15.0
    max_retries: int = 2
    retry_backoff: float = 0.8
    user_agent: str = USER_AGENT
    audit_log: list[CallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.token:
            # 不在异常里回显 token 内容，避免凭据进入日志。
            raise McDError(
                McDErrorKind.UNSUPPORTED,
                "未配置 MCD_MCP_TOKEN 环境变量，无法连接麦当劳 MCP。",
            )


class McDClient:
    """麦当劳 MCP Client。

    只暴露业务需要的只读方法。任何写操作（下单、领券、抽奖、取消订单）
    都刻意不提供实现，从代码层面保证本项目不会产生真实交易。
    """

    #: 明确禁止调用的工具名。这些属于交易/消费类操作，本项目不实现。
    FORBIDDEN_TOOLS = frozenset(
        {
            "create-order",
            "cancel-order",
            "auto-bind-coupons",
            "draw-lottery",
            "mall-create-order",
            "party-order-create",
            "delivery-create-address",
        }
    )

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._request_id = 0

    # ------------------------------------------------------------------
    # 协议层
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送一次 JSON-RPC 请求，返回解析后的 JSON-RPC 响应体。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._config.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": self._config.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raise McDError(
                McDErrorKind.HTTP_STATUS,
                f"麦当劳 MCP 返回 HTTP {status}",
                retryable=status in _RETRYABLE_HTTP_STATUS,
                detail=status,
            ) from exc
        except TimeoutError as exc:
            raise McDError(McDErrorKind.TIMEOUT, "麦当劳 MCP 响应超时", retryable=True) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                raise McDError(McDErrorKind.TIMEOUT, "麦当劳 MCP 响应超时", retryable=True) from exc
            raise McDError(McDErrorKind.TRANSPORT, f"无法连接麦当劳 MCP：{reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McDError(
                McDErrorKind.PARSE, "麦当劳 MCP 响应不是合法 JSON", detail=raw[:400]
            ) from exc

    @staticmethod
    def _extract_payload(response: dict[str, Any]) -> tuple[Any, str | None, str | None]:
        """从 JSON-RPC 响应中取出业务数据、服务器时间与 traceId。

        麦当劳 MCP 会把结构说明和真实 JSON 一起塞进 content[].text，
        真实 JSON 位于 "## Original Response" 之后。

        重要：部分工具（如 available-coupons）返回的是 Markdown 纯文本而非
        JSON。这类工具解析失败时返回原始文本，由调用方按文本处理，
        而不是在这里抛异常 —— 否则会静默丢掉真实数据。
        """
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise McDError(McDErrorKind.RPC, f"麦当劳 MCP 返回错误：{message}", detail=error)

        result = response.get("result")
        if not isinstance(result, dict):
            raise McDError(McDErrorKind.PARSE, "麦当劳 MCP 响应缺少 result 字段")

        contents = result.get("content") or []
        text = ""
        for block in contents:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                break

        if not text:
            return None, None, None

        body = _extract_json_body(text)
        if body is None:
            # 文本型工具（如 available-coupons 返回 Markdown）：原样返回，
            # 由调用方按文本处理。这不是错误。
            return text.strip(), None, None

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return text.strip(), None, None

        server_datetime = payload.get("datetime") if isinstance(payload, dict) else None
        trace_id = payload.get("traceId") if isinstance(payload, dict) else None
        return payload, server_datetime, trace_id

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> tuple[Any, CallRecord]:
        """调用一个麦当劳 MCP 工具，返回 (业务数据, 审计记录)。

        这是项目内部唯一的 MCP 调用入口。包含有限次重试，
        绝不会无限循环。
        """
        if tool in self.FORBIDDEN_TOOLS:
            raise McDError(
                McDErrorKind.UNSUPPORTED,
                f"工具 {tool} 属于交易类操作，本项目为只读决策辅助，不予实现。",
                tool=tool,
            )

        arguments = arguments or {}
        attempts = self._config.max_retries + 1
        last_error: McDError | None = None

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self._post(
                    {
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    }
                )
                payload, server_datetime, trace_id = self._extract_payload(response)
                latency_ms = int((time.monotonic() - started) * 1000)

                if isinstance(payload, dict) and payload.get("success") is False:
                    code = payload.get("code")
                    message = payload.get("message") or "业务处理失败"
                    raise McDError(
                        McDErrorKind.BUSINESS,
                        f"麦当劳系统返回 code={code}：{message}",
                        tool=tool,
                        detail=payload,
                    )

                record = CallRecord(
                    tool=tool,
                    arguments=arguments,
                    ok=True,
                    latency_ms=latency_ms,
                    server_datetime=server_datetime,
                    trace_id=trace_id,
                )
                self._config.audit_log.append(record)
                return payload, record
            except McDError as exc:
                exc.tool = exc.tool or tool
                last_error = exc
                latency_ms = int((time.monotonic() - started) * 1000)
                self._config.audit_log.append(
                    CallRecord(
                        tool=tool,
                        arguments=arguments,
                        ok=False,
                        latency_ms=latency_ms,
                        error_kind=exc.kind,
                        error_message=str(exc),
                    )
                )
                if not exc.retryable or attempt >= attempts:
                    raise
                backoff = self._config.retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "麦当劳 MCP %s 第 %s 次失败（%s），%.1fs 后重试",
                    tool,
                    attempt,
                    exc.kind,
                    backoff,
                )
                time.sleep(backoff)

        raise last_error or McDError(McDErrorKind.TRANSPORT, "麦当劳 MCP 调用失败")

    def list_tools(self) -> list[dict[str, Any]]:
        """返回服务端声明的工具清单，用于能力自检与文档生成。"""
        response = self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        )
        if "error" in response:
            raise McDError(McDErrorKind.RPC, "tools/list 调用失败")
        return (response.get("result") or {}).get("tools") or []

    def audit_records(self) -> list[dict[str, Any]]:
        """导出审计记录，供集成验证证据使用。"""
        return [record.to_dict() for record in self._config.audit_log]