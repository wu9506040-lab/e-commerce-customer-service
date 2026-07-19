"""
ci_env_diff.py - CI 部署环境一致性检查（防 M3 部署 bug 重演）

M3 部署治本（2026-07-19 · commit dfa905f）发现部署层 bug：
- `deploy/.env` ECS 上 MYSQL_PASSWORD=cs_pass_2026（占位）
- MySQL 容器 init 时实际密码=dev_user_2026
- → API 容器连接 MySQL 被拒（auth 失败）
- 根因：`.env` 与 `.env.dev` 分叉无 review 机制 + 路径分离（评测通过 ≠ 部署通过）

本脚本任务：
1. 防占位符：扫描所有 .env 文件，发现黑名单 placeholder 密码立刻退出 1
2. 防分叉：对比 .env 与 .env.dev / .env.prod.example，找出仅在一侧存在的 key
3. 给 PR diff 反馈（人在 review 时一眼看到 drift）

用法：
  python scripts/ci_env_diff.py             # 默认检查所有 deploy/.env*
  python scripts/ci_env_diff.py --strict    # 严格模式（placeholder 直接 fail）
  python scripts/ci_env_diff.py --echo      # 打印所有 diff（debug）

CI 集成（.github/workflows/ci.yml）：
  - name: Env diff check
    run: python scripts/ci_env_diff.py --strict
"""
import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# === 配置 ===
DEPLOY_DIR = Path("deploy")
PRIMARY_ENV_FILES = [
    DEPLOY_DIR / ".env",
    DEPLOY_DIR / ".env.dev",
    DEPLOY_DIR / ".env.example",
    DEPLOY_DIR / ".env.prod.example",
]

# 占位符黑名单（防 prod validator 触发 "APP_ENV=prod 环境下，DATABASE_URL 含占位符密码"）
PLACEHOLDER_PASSWORDS = {
    "dev_user_2026",
    "rootpass_cs_2026",
    "change_me",
    "password",
    "secret",
    "changethis",
    "example",
    "xxx",
    "todo",
}

# 关键密钥 key（必须设，非空，且不能是 placeholder）
SENSITIVE_KEYS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "REDIS_PASSWORD",
    "QDRANT_API_KEY",
    "QWEN_API_KEY",
    "JWT_SECRET",
    "DASHSCOPE_API_KEY",
)


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析 .env 文件（key=value 格式 · 忽略 # 注释和空行）"""
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    return env


def check_placeholders(env_files: List[Path], strict: bool) -> List[str]:
    """扫描占位符密码 · 返回 issue 列表

    注意：模板文件（.env.example / .env.prod.example）的占位符是预期的，不 flag。
    只对真实使用的 .env 文件（.env.dev / .env）做 placeholder 检查。
    """
    issues: List[str] = []
    for path in env_files:
        if not path.exists():
            continue
        # 模板文件 placeholder 是预期的，跳过
        if path.name in (".env.example", ".env.prod.example", ".env.example.bak"):
            continue
        env = parse_env_file(path)
        for key, value in env.items():
            if any(p in value.lower() for p in PLACEHOLDER_PASSWORDS):
                # 仅在 primary sensitive keys 上 flag
                if key in {"MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "REDIS_PASSWORD",
                           "JWT_SECRET", "QWEN_API_KEY", "QDRANT_API_KEY", "DASHSCOPE_API_KEY"}:
                    issue = f"[PLACEHOLDER] {path.name}:{key}='{value[:8]}...' (matched blacklist)"
                    issues.append(issue)
    return issues


def check_drift(env_files: List[Path]) -> List[str]:
    """检查 env 文件之间的 key 一致性 · 返回 issue 列表

    注意：模板文件（*.example）的 key 不参与 drift 检查，因为模板按需精简。
    只对真实 .env 类文件（.env.dev / .env / .env.bak）做 drift 检测。
    """
    issues: List[str] = []
    # 模板文件分离
    template_files = [p for p in env_files if "example" in p.name]
    real_files = [p for p in env_files if "example" not in p.name]

    # 1. 真实文件之间 drift
    if len(real_files) >= 2:
        all_envs: Dict[str, Dict[str, str]] = {
            path.name: parse_env_file(path)
            for path in real_files if path.exists()
        }
        all_keys: Set[str] = set()
        for env in all_envs.values():
            all_keys.update(env.keys())

        for key in sorted(all_keys):
            present_in = [name for name, env in all_envs.items() if key in env]
            absent_in = [name for name, env in all_envs.items() if key not in env]
            if len(present_in) > 0 and len(absent_in) > 0:
                issues.append(
                    f"[DRIFT] {key}: 仅 {present_in} 出现，{absent_in} 缺失"
                )

        # 同 key 不同值（高亮敏感 key）
        for key in sorted(all_keys):
            values = {name: env.get(key) for name, env in all_envs.items() if key in env}
            unique_values = set(values.values())
            if len(unique_values) > 1:
                if key in SENSITIVE_KEYS or "PASSWORD" in key or "SECRET" in key or "KEY" in key:
                    lines = ", ".join(f"{n}='{v[:8]}...'" for n, v in values.items())
                    issues.append(f"[VALUE-DIFF] {key}: {lines}")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="CI env drift + placeholder check")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式：placeholder 直接 exit 1")
    parser.add_argument("--echo", action="store_true",
                        help="打印所有 diff 内容（debug 用）")
    parser.add_argument("--files", nargs="*",
                        help="指定 env 文件路径（默认扫描 deploy/.env*）")
    args = parser.parse_args()

    # 决定要扫的文件
    if args.files:
        env_files = [Path(f) for f in args.files]
    else:
        env_files = [p for p in PRIMARY_ENV_FILES if p.exists()]

    print(f"[ci_env_diff] 检查 {len(env_files)} 个 env 文件: "
          f"{[p.name for p in env_files]}")

    # 1. placeholder check
    placeholder_issues = check_placeholders(env_files, strict=args.strict)
    # 2. drift check
    drift_issues = check_drift(env_files)

    all_issues = placeholder_issues + drift_issues

    if args.echo or all_issues:
        if all_issues:
            print(f"\n[ci_env_diff] 发现 {len(all_issues)} 个问题:")
            for issue in all_issues:
                print(f"  - {issue}")
        else:
            print("\n[ci_env_diff] [OK] 无问题（所有 env 文件一致）")

    if not all_issues:
        return 0

    if args.strict and placeholder_issues:
        print(f"\n[ci_env_diff] [FAIL] STRICT 模式：placeholder 检测失败，exit 1")
        return 1

    # 非严格模式：仅 print 但不 fail（PR review 时给信息）
    print(f"\n[ci_env_diff] [WARN] 发现 drift，但仅 print 不 exit（非 strict 模式）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
