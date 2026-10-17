"""零依赖测试运行器。

本项目的核心测试不依赖 pytest，因此在没有网络、没有第三方包的环境下
也能直接验证逻辑。pytest 存在时仍可正常使用（见 tests/test_mcchoice.py）。

用法：
    python tests/run_tests.py
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from tests.test_edge_cases import (  # noqa: E402
    TestAllergySafety,
    TestBudgetExtraction,
    TestMedicalDietNeeds,
    TestCustomerProfiles,
    TestNutritionScoring,
    TestChineseNumbers,
    TestFuzzNoCrash,
    TestPartySizeLimits,
    TestTextMatchingPriority,
)
from tests.test_mcchoice import (  # noqa: E402
    TestClientSafety,
    TestComboPlanning,
    TestHardConstraints,
    TestIntentParsing,
    TestNutrition,
    TestScoring,
    TestSkillEndToEnd,
)

TEST_CLASSES = [
    TestIntentParsing,
    TestChineseNumbers,
    TestBudgetExtraction,
    TestPartySizeLimits,
    TestTextMatchingPriority,
    TestFuzzNoCrash,
    TestMedicalDietNeeds,
    TestAllergySafety,
    TestCustomerProfiles,
    TestNutritionScoring,
    TestHardConstraints,
    TestScoring,
    TestComboPlanning,
    TestNutrition,
    TestSkillEndToEnd,
    TestClientSafety,
]

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def _run_method(instance: Any, method: Callable[..., Any]) -> tuple[bool, str]:
    try:
        method(instance)
        return True, ""
    except Exception:  # noqa: BLE001
        return False, traceback.format_exc()


def main() -> int:
    passed = 0
    failed: list[tuple[str, str]] = []
    skipped = 0

    for test_class in TEST_CLASSES:
        methods = [
            (name, getattr(test_class, name))
            for name in dir(test_class)
            if name.startswith("test_")
        ]
        if not methods:
            continue

        print(f"\n{DIM}{test_class.__name__}{RESET}")
        for name, method in methods:
            # 跳过依赖 pytest.raises 的测试（那类在 TestClientSafety 里单独标注）
            if getattr(method, "__code__", None) and "pytest" in method.__code__.co_names:
                print(f"  {YELLOW}SKIP{RESET} {name} {DIM}(需 pytest){RESET}")
                skipped += 1
                continue

            instance = test_class()
            ok, error = _run_method(instance, method)
            if ok:
                passed += 1
                print(f"  {GREEN}PASS{RESET} {name}")
            else:
                failed.append((f"{test_class.__name__}::{name}", error))
                print(f"  {RED}FAIL{RESET} {name}")

    total = passed + len(failed)
    print(f"\n{'=' * 56}")
    if failed:
        print(f"{RED}{len(failed)} failed{RESET}, {GREEN}{passed} passed{RESET}, {skipped} skipped")
        print(f"{'=' * 56}\n")
        for name, error in failed:
            print(f"\n{RED}FAILED{RESET} {name}")
            print(DIM + error + RESET)
        return 1

    print(f"{GREEN}全部通过{RESET}: {passed} passed, {skipped} skipped")
    print(f"{'=' * 56}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())