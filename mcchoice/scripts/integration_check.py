"""真实 MCP 集成验证脚本。

用途：证明本项目确实调用了真实的麦当劳 MCP，而不是编造数据。
运行前必须设置环境变量 MCD_MCP_TOKEN。

用法：
    export MCD_MCP_TOKEN="<your-token>"
    python scripts/integration_check.py --city 南京 --keyword 新街口 --query "30元以内随便吃"

本脚本只调用只读工具，不下单、不领券、不抽奖。
它会打印每一步的真实调用记录与 traceId，便于在参赛材料中作为证据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcchoice.adapters.mcd_client import ClientConfig, McDClient, McDError  # noqa: E402
from mcchoice.adapters.service import McDService  # noqa: E402
from mcchoice.skill import McChoiceSkill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="McChoice 真实 MCP 集成验证")
    parser.add_argument("--city", default="南京")
    parser.add_argument("--keyword", default="新街口")
    parser.add_argument("--query", default="随便推荐一个")
    parser.add_argument("--dump-audit", default="", help="把审计记录写入指定 JSON 文件")
    args = parser.parse_args()

    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置环境变量 MCD_MCP_TOKEN", file=sys.stderr)
        return 2

    config = ClientConfig(token=token)
    client = McDClient(config)
    service = McDService(client)

    print("=" * 60)
    print("McChoice 真实 MCP 集成验证")
    print("=" * 60)

    # 1. 能力自检
    try:
        tools = client.list_tools()
        print(f"[1] tools/list 成功，服务端共开放 {len(tools)} 个工具")
    except McDError as exc:
        print(f"[1] tools/list 失败：{exc}", file=sys.stderr)
        return 1

    # 2. 完整决策流程
    skill = McChoiceSkill(service, enable_coupon_lookup=True)
    print(f"\n[2] 执行决策：{args.query}（{args.city} {args.keyword}）\n")
    try:
        result = skill.decide(args.query, city=args.city, keyword=args.keyword)
    except McDError as exc:
        print(f"决策失败：{exc.user_facing_note()}", file=sys.stderr)
        return 1

    print(result.answer)

    print("\n" + "=" * 60)
    print(f"候选池：{result.candidates_considered} 个，通过硬约束：{result.candidates_passed} 个")
    if result.warnings:
        print(f"告警：{'；'.join(result.warnings)}")

    # 3. 审计记录
    records = client.audit_records()
    print(f"\n真实 MCP 调用记录（共 {len(records)} 次）：")
    for record in records:
        status = "OK " if record["ok"] else "ERR"
        trace = record.get("traceId") or "-"
        latency = record["latencyMs"]
        print(f"  [{status}] {record['tool']:<24} {latency:>5}ms  traceId={trace}")
        if not record["ok"]:
            print(f"        错误类型：{record.get('errorKind')} - {record.get('errorMessage')}")

    if args.dump_audit:
        with open(args.dump_audit, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        print(f"\n审计记录已写入：{args.dump_audit}")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())