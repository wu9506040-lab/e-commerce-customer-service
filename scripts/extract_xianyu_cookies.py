"""extract_xianyu_cookies.py - 从 Edge/Chrome SQLite 提取闲鱼 cookies → WSL Linux 路径

用法: python scripts/extract_xianyu_cookies.py

流程:
  1. 找 Edge/Chrome Cookies SQLite DB
  2. DPAPI (CryptUnprotectData) 解密 cookie value
  3. 输出 JSON 数组到 /tmp/xianyu_cookies.json
  4. 拷贝到 WSL Ubuntu /home/zw/.goofish-cli/cookies_raw.json
  5. 在 WSL 内跑 goofish auth login 转换格式（plaintext JSON → goofish 加密 JSON）

要求: pywin32 (pip install pywin32) + 系统 sqlite3 + WSL Ubuntu
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import win32crypt
except ImportError:
    print("ERROR: 需要 pywin32。运行: pip install pywin32", file=sys.stderr)
    sys.exit(1)


def find_cookie_db():
    """找 Edge/Chrome Cookies SQLite（找最新版本，优先 Network/Cookies）"""
    appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("Edge", Path(appdata) / "Microsoft" / "Edge" / "User Data" / "Default" / "Network" / "Cookies"),
        ("Edge", Path(appdata) / "Microsoft" / "Edge" / "User Data" / "Default" / "Cookies"),
        ("Chrome", Path(appdata) / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies"),
        ("Chrome", Path(appdata) / "Google" / "Chrome" / "User Data" / "Default" / "Cookies"),
    ]
    for name, path in candidates:
        if path.exists():
            print(f"Use {name} cookies DB: {path}")
            return name, path
    raise FileNotFoundError(
        f"找不到 Edge/Chrome cookies DB。先在浏览器登录 https://www.goofish.com"
    )


def decrypt_value(encrypted: bytes) -> str:
    """DPAPI 解密 cookie value（Chrome v10+ 用 v10 prefix，v8 直接 DPAPI）"""
    if not encrypted:
        return ""
    if encrypted[:3] == b"v10":
        # Chrome v10: DPAPI( AES-GCM(encrypted, key) )，需要从 Local State 解 key
        # 简化：v10 在 Windows 上 DPAPI 解密后是 32 字节 nonce+tag+密文
        # 但 goofish cli 不在乎格式（只读 unb / _m_h5_tk），我们只关心这两个 cookie 的 value
        # 实际上 v10 解密需 key from Local State — 复杂。
        # 退路：跳过 v10（多数 cookie 仍 v10，但 unb/_m_h5_tk 是 httpOnly 通常 v8）
        raise ValueError("v10 encrypted — 跳过（需要 Local State key）")
    # v8: 直接 DPAPI
    return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)


def extract_xianyu_cookies():
    name, db_path = find_cookie_db()

    # 直接以 mode=ro URI 读（SQLite 允许并发读，写锁 Edge 不影响）
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    cur = conn.cursor()
    # v10 cookie 跳过（需要 Local State key 解密）；unb/_m_h5_tk 通常 v8
    cur.execute(
        "SELECT name, host_key, path, encrypted_value "
        "FROM cookies WHERE host_key LIKE '%goofish%' "
        "AND encrypted_value != ''"
    )
    records = []
    v10_skipped = 0
    for name_, host, path, enc in cur.fetchall():
        try:
            value = decrypt_value(enc)
        except ValueError:
            v10_skipped += 1
            continue
        except Exception as e:
            print(f"warn: decrypt {name_} failed: {e}")
            continue
        records.append({
            "name": name_, "value": value,
            "domain": host, "path": path,
        })
    conn.close()

    print(f"Extracted {len(records)} cookies (v10 skipped: {v10_skipped})")
    if not records:
        raise RuntimeError("No cookies extracted; 确认浏览器已登录闲鱼")
    return records


def write_to_wsl(records):
    """写到 WSL Ubuntu 的 /home/zw/.goofish-cli/cookies_raw.json"""
    # 先写 Windows 本地 /tmp，再 Copy-Item 到 WSL UNC 路径
    win_tmp = Path(os.environ.get("TEMP", "C:/Users/zw/AppData/Local/Temp")) / "xianyu_cookies_raw.json"
    win_tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    wsl_path = Path(r"\\wsl$\Ubuntu\home\zw\.goofish-cli\cookies_raw.json")
    wsl_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(win_tmp, wsl_path)
    win_tmp.unlink()
    print(f"Wrote {wsl_path}")


def wsl_run(cmd: str) -> tuple[int, str]:
    """在 WSL Ubuntu 跑 bash"""
    r = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd],
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    records = extract_xianyu_cookies()

    # 显示关键 cookie
    important = {"unb", "_m_h5_tk", "cookie2", "sgcookie", "t", "_tb_token_"}
    print("\n=== Key cookies ===")
    for r in records:
        if r["name"] in important:
            v = r["value"][:40] + "..." if len(r["value"]) > 40 else r["value"]
            print(f"  {r['name']:20} = {v}")

    write_to_wsl(records)

    # 跑 goofish auth login
    print("\n=== WSL: goofish auth login ===")
    code, out = wsl_run(
        "export PATH=/home/zw/.local/bin:$PATH; "
        "goofish auth login /home/zw/.goofish-cli/cookies_raw.json; "
        "echo EXIT=$?"
    )
    print(out)

    print("\n=== WSL: goofish auth status ===")
    code, out = wsl_run(
        "export PATH=/home/zw/.local/bin:$PATH; "
        "timeout 10 goofish auth status --format json"
    )
    print(out)


if __name__ == "__main__":
    main()
