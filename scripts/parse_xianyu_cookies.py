"""
Edge/Chrome Cookie header → 闲鱼 goofish JSON 字典转换

用法：
  1. Edge DevTools → Network 标签 → 点任一 *.goofish.com / m.goofish.com 请求
  2. 右侧 Headers → Request Headers → 找 Cookie 字段 → 整段复制
  3. 保存到 C:/Users/zw/goofish-cookie-header.txt
  4. python scripts/parse_xianyu_cookies.py C:/Users/zw/goofish-cookie-header.txt

输出：C:/Users/zw/goofish-cookies.json（dict 格式，goofish 自动加密写盘）
"""
import json
import sys
from pathlib import Path


# 闲鱼 IM 关键 cookie（必须含 _m_h5_tk + unb；其余可选项保留）
_KEY_NAMES = {
    "_m_h5_tk",
    "_m_h5_tk_enc",
    "_tb_token_",
    "unb",
    "tracknick",
    "cookie2",
    "sgcookie",
    "t_sid",
    "isg",
    "lgc",
    "cna",
    "tfstk",
}


def parse_cookie_header(text: str) -> dict[str, str]:
    """'a=1; b=2; c=3' → {'a': '1', 'b': '2', 'c': '3'}"""
    cookies: dict[str, str] = {}
    for part in text.replace("\n", "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookies[name] = value
    return cookies


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "用法：python scripts/parse_xianyu_cookies.py <cookie-header.txt> [output.json]"
        )
        return 2

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"❌ 文件不存在：{src}")
        return 1

    text = src.read_text(encoding="utf-8", errors="replace")
    cookies = parse_cookie_header(text)

    if not cookies:
        print(f"❌ 未解析到任何 cookie（{src} 是空或格式不对）")
        return 1

    # 校验关键字段
    missing = [k for k in ("_m_h5_tk", "unb") if k not in cookies]
    if missing:
        print(f"⚠️ 缺少关键字段：{missing}")
        print("可能不是 goofish.com 域名的 Cookie header。goofish 会拒绝。")

    dst = (
        Path(sys.argv[2])
        if len(sys.argv) >= 3
        else Path.home() / "goofish-cookies.json"
    )
    dst.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 解析到 {len(cookies)} 个 cookie")
    print(f"   关键字段：", end="")
    print(
        ", ".join(
            f"{k}={'<set>' if k in cookies else '<missing>'}"
            for k in ("_m_h5_tk", "unb", "tracknick", "_tb_token_")
        )
    )
    print(f"✅ 写入 {dst}")
    print()
    print("下一步：")
    print(f"  goofish auth login {dst}")
    print(f"  goofish auth status   # 验证登录态")
    return 0


if __name__ == "__main__":
    sys.exit(main())