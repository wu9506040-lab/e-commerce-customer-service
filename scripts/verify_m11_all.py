"""
M11+M11.5 全量集成测试入口

依次跑通 3 个 verify 脚本：
  1. verify_guard.py    - InputGuard 3 层防御
  2. verify_cache.py    - 响应缓存 (exact + semantic)
  3. verify_behavior.py - 异常行为监控

每个脚本独立清理 Redis 不冲突，跑完合并报结果。

用法：
    python scripts/verify_m11_all.py
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS = [
    ("verify_guard.py",    "InputGuard 3 层防御"),
    ("verify_cache.py",    "响应缓存 (exact + semantic)"),
    ("verify_behavior.py", "异常行为监控 5 类告警"),
]


def run_one(script: str, name: str) -> tuple[bool, str]:
    """跑一个 verify 脚本，返回 (passed, output)"""
    print("\n" + "#" * 70)
    print(f"# RUNNING: {name} ({script})")
    print("#" * 70)
    p = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script)],
        cwd=str(PROJECT_ROOT.parent),  # 项目根
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = p.stdout + p.stderr
    print(output[-2500:] if len(output) > 2500 else output)
    return p.returncode == 0, output


def main() -> int:
    print("=" * 70)
    print("M11 + M11.5 全量集成测试")
    print("=" * 70)

    results = []
    for script, name in SCRIPTS:
        passed, _ = run_one(script, name)
        results.append((name, passed))

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    for name, passed in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"\n  Total: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
