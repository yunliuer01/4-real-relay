# -*- coding: utf-8 -*-
"""产品换绑到本组专用网关 g5-mqtt-gateway（只操作本组的 mqtt-iot 产品和 FILE-TERM-01 设备）"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9000/api"
NEW_GATEWAY = "2095085346338361344"  # g5-mqtt-gateway


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
        return "OK " + json.dumps(r, ensure_ascii=False)[:200]
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read(250).decode('utf-8', 'replace')}"
    except Exception as e:
        return f"ERR {e}"


def main():
    token = req("POST", "/authorize/login",
                {"username": "admin5", "password": "Admin@group5"})["result"]["token"]

    # 1. 备份设备完整记录
    d = req("GET", "/device/instance/FILE-TERM-01/detail", token=token)["result"]
    backup = json.dumps(d, ensure_ascii=False, indent=1)
    with open("data/device_backup.json", "w", encoding="utf-8") as f:
        f.write(backup)
    print("1. 设备记录已备份到 data/device_backup.json")

    # 2. 删除本组设备实例
    print("2. 删除设备 ->", try_req("DELETE", "/device/instance/FILE-TERM-01", token=token))

    # 3. 产品换绑到本组网关
    p = req("POST", "/device/product/_query/no-paging",
            {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
            token=token)["result"][0]
    p["accessId"] = NEW_GATEWAY
    p["accessName"] = "g5-mqtt-gateway"
    p["accessProvider"] = "mqtt-client-gateway"
    print("3. 产品换绑 ->", try_req("PUT", "/device/product/mqtt-iot", p, token))

    # 4. 重建设备（同 ID、同产品）
    new_dev = {
        "id": "FILE-TERM-01",
        "name": d.get("name", "文件监听温湿度终端"),
        "productId": "mqtt-iot",
        "productName": "MQTT温湿度终端",
        "describe": d.get("describe", ""),
        "deviceType": {"value": "device", "text": "直连设备"},
        "configuration": d.get("configuration", {}) or {},
    }
    print("4. 重建设备 ->", try_req("POST", "/device/instance", new_dev, token))

    # 5. 激活（部署）设备
    print("5. 激活设备 ->", try_req("POST", "/device/instance/FILE-TERM-01/deploy", {}, token))

    # 6. 确认
    try:
        d2 = req("GET", "/device/instance/FILE-TERM-01/detail", token=token)["result"]
        print("\n设备重建成功:", d2.get("id"), d2.get("name"),
              " 状态:", d2.get("state", {}).get("value"))
    except Exception as e:
        print("确认失败:", e)
    try:
        p2 = req("POST", "/device/product/_query/no-paging",
                 {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
                 token=token)["result"][0]
        print("产品最终 accessId:", p2.get("accessId"), "(", p2.get("accessName"), ")")
    except Exception as e:
        print("产品确认失败:", e)


if __name__ == "__main__":
    main()
