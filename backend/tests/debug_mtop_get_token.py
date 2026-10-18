"""
debug_mtop_get_token.py — V17 调试脚本

直接调 mtop.taobao.idlemessage.pc.login.token，看真实返回结构
确认 accessToken 在 data.X 还是别的位置
"""
import sys
import time
import json
import hashlib
import requests
from pathlib import Path

# 读 cookie.txt
cookie_path = Path("E:/智能客服/cookie.txt")
cookies_str = cookie_path.read_text(encoding="utf-8").strip()
print(f"[1] Cookie 长度: {len(cookies_str)} chars")

# 解析
cookies = dict(c.split("=", 1) for c in cookies_str.split("; "))
tk_full = cookies["_m_h5_tk"]
token = tk_full.split("_")[0]
print(f"[2] _m_h5_tk full: {tk_full}")
print(f"[3] sign token: {token}")
print(f"[4] unb: {cookies.get('unb')}")

# device_id 模拟
device_id = "ED4CBA2C-5DA0-4154-A902-BF5CB52409E2-3888777108"

# mtop params
t = str(int(time.time()) * 1000)
data_val = json.dumps(
    {"appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": device_id},
    separators=(",", ":"),
)
msg = f"{token}&{t}&34839810&{data_val}"
sign = hashlib.md5(msg.encode("utf-8")).hexdigest()

url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
params = {
    "jsv": "2.7.2",
    "appKey": "34839810",
    "v": "1.0",
    "type": "originaljson",
    "accountSite": "xianyu",
    "dataType": "json",
    "timeout": "20000",
    "api": "mtop.taobao.idlemessage.pc.login.token",
    "sessionOption": "AutoLoginOnly",
    "t": t,
    "sign": sign,
}
headers = {
    "Host": "h5api.m.goofish.com",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "accept": "application/json",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "content-type": "application/x-www-form-urlencoded",
    "sec-ch-ua-mobile": "?0",
    "origin": "https://www.goofish.com",
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "referer": "https://www.goofish.com/",
    "accept-language": "zh-CN,zh;q=0.9",
    "priority": "u=1, i",
}

print(f"\n[5] POST {url}")
print(f"    data_val: {data_val}")
print(f"    sign: {sign}")

try:
    resp = requests.post(
        url,
        params=params,
        data={"data": data_val},
        headers=headers,
        cookies=cookies,
        timeout=15,
    )
    print(f"\n[6] HTTP {resp.status_code}")
    print(f"    Set-Cookie: {dict(resp.cookies)}")
    resp_json = resp.json()
    print(f"\n[7] Response JSON:")
    print(json.dumps(resp_json, indent=2, ensure_ascii=False))

    # 找 accessToken 的实际位置
    print(f"\n[8] accessToken 查找:")
    for path in [
        ("data",),
        ("data", "accessToken"),
        ("data", "result", "accessToken"),
        ("data", "result", "data", "accessToken"),
        ("accessToken",),
        ("data", "data", "accessToken"),
    ]:
        cur = resp_json
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if cur and isinstance(cur, str) and len(cur) > 10:
            print(f"    {list(path)}: {cur[:50]}...")
        elif cur:
            print(f"    {list(path)}: {cur!r}")

    # 检查 ret 字段
    print(f"\n[9] ret 字段:")
    ret = resp_json.get("ret", [])
    print(f"    {ret}")

except Exception as e:
    print(f"\n[ERROR] {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()