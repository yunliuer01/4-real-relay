# -*- coding: utf-8 -*-
"""在 mqtt-iot 产品下新增 Modbus 采集设备 MODBUS-TERM-01 并激活（不新建产品）
与 FILE-TERM-01 同属一个产品，平台侧一个产品两个设备。
"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9000/api"
DEVICE_ID = "MODBUS-TERM-01"
PRODUCT_ID = "mqtt-iot"


def req(method, path, data=None, token=None):
    r = urllib.request.Request(BASE + path,
                               data=json.dumps(data).encode() if data is not None else None,
                               headers={"Content-Type": "application/json"})
    r.get_method = lambda: method
    if token:
        r.add_header("X-Access-Token", token)
    return json.load(urllib.request.urlopen(r, timeout=30))


def try_req(method, path, data=None, token=None):
    try:
        r = req(method, path, data, token)
        return "OK " + json.dumps(r, ensure_ascii=False)[:300]
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read(250).decode('utf-8', 'replace')}"
    except Exception as e:
        return f"ERR {e}"


def main():
    token = req("POST", "/authorize/login",
                {"username": "admin5", "password": "Admin@group5"})["result"]["token"]

    # 0. 若已存在则跳过
    exists = req("POST", "/device/instance/_query/no-paging",
                 {"paging": False, "terms": [{"column": "id", "value": DEVICE_ID}]},
                 token=token)["result"]
    if exists:
        d = exists[0]
        print(f"设备 {DEVICE_ID} 已存在，当前 state={d.get('state', {}).get('value')}")
        if str(d.get("state", {}).get("value")) != "online":
            print("  尝试激活 ->", try_req("POST", f"/device/instance/{DEVICE_ID}/deploy", {}, token))
        return

    new_dev = {
        "id": DEVICE_ID,
        "name": "Modbus采集终端-lfx",
        "productId": PRODUCT_ID,
        "productName": "MQTT温湿度终端",
        "describe": "Modbus TCP 轮询采集温湿度，经 EMQX 规则转换上报 mqtt-iot 产品",
        "deviceType": {"value": "device", "text": "直连设备"},
        "configuration": {},
    }
    print("1. 创建设备 ->", try_req("POST", "/device/instance", new_dev, token))
    print("2. 激活设备 ->", try_req("POST", f"/device/instance/{DEVICE_ID}/deploy", {}, token))

    # 3. 确认
    try:
        d2 = req("GET", f"/device/instance/{DEVICE_ID}/detail", token=token)["result"]
        print("设备创建成功:", d2.get("id"), d2.get("name"),
              " 产品:", d2.get("productId"),
              " 状态:", d2.get("state", {}).get("value"))
    except Exception as e:
        print("确认失败:", e)


if __name__ == "__main__":
    main()
