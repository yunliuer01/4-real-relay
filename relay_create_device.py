# -*- coding: utf-8 -*-
"""Day3/4 任务：在产品 relay8_lfx 下创建 8 路继电器设备 RELAY8-TERM-01 并激活
参考已完成的 relay4_mt 设备：configuration secureId/secureKey=设备ID, plaintext
"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9000/api"
DEVICE_ID = "RELAY8-TERM-01"
PRODUCT_ID = "relay8_lfx"


def req(method, path, data=None, token=None, timeout=30):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("X-Access-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    _, r = req("POST", "/authorize/login",
               {"username": "admin5", "password": "Admin@group5"})
    token = r["result"]["token"]
    print("登录 OK")

    # 已存在则跳过
    code, r = req("POST", "/device/instance/_query/no-paging",
                  {"terms": [{"column": "id", "value": DEVICE_ID}]}, token)
    res = r.get("result")
    devs = res if isinstance(res, list) else (res or {}).get("data", [])
    if devs:
        d = devs[0]
        st = d.get("state")
        print(f"设备已存在: state={st.get('value') if isinstance(st, dict) else st}")
        if str(st.get("value") if isinstance(st, dict) else st) != "online":
            code, r = req("POST", f"/device/instance/{DEVICE_ID}/deploy", {}, token)
            print("激活 ->", code, json.dumps(r, ensure_ascii=False)[:150])
        return

    new_dev = {
        "id": DEVICE_ID,
        "name": "8路继电器终端-lfx",
        "productId": PRODUCT_ID,
        "productName": "8路继电器-lfx",
        "describe": "Day3/4 8路继电器模拟器：EMQX 规则转换 /relay8_lfx/{deviceId}/property/post -> properties/report，支持 write 功能控制",
        "deviceType": {"value": "device", "text": "直连设备"},
        "configuration": {"secureId": DEVICE_ID, "secureKey": DEVICE_ID,
                          "secureType": "plaintext"},
    }
    code, r = req("POST", "/device/instance", new_dev, token)
    print("1. 创建设备 ->", code, json.dumps(r, ensure_ascii=False)[:200])
    code, r = req("POST", f"/device/instance/{DEVICE_ID}/deploy", {}, token)
    print("2. 激活设备 ->", code, json.dumps(r, ensure_ascii=False)[:200])

    code, r = req("GET", f"/device/instance/{DEVICE_ID}/detail", None, token)
    if code == 200:
        d = r.get("result")
        st = d.get("state")
        print("设备确认:", d.get("id"), d.get("name"), "产品:", d.get("productId"),
              "状态:", st.get("value") if isinstance(st, dict) else st)


if __name__ == "__main__":
    main()
