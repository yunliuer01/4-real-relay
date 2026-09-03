# -*- coding: utf-8 -*-
"""Day3/4 任务：新建 8 路继电器产品 relay8_lfx（不动 mqtt-iot 温湿度产品）
物模型：属性 r1..r8（enum 1=开/0=关）+ 功能 write（写继电器，8 个开关输入）
接入方式：mqtt 接入网关 2092562181967769600（与 mqtt-iot / 参考组 relay4_mt 一致）
"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9000/api"
PRODUCT_ID = "relay8_lfx"
MQTT_PROTOCOL_ID = "2092561848730316800"     # mqtt 协议（平台内置 demo）
MQTT_ACCESS_ID = "2092562181967769600"       # mqtt接入 网关（平台内置 demo）


def build_metadata():
    """构造与参考产品 relay4_mt 同构、扩展为 8 路的物模型"""
    props = []
    for i in range(1, 9):
        props.append({
            "id": f"r{i}",
            "name": f"开关{i}",
            "expands": {"source": "device", "type": ["report"],
                        "groupId": "group_1", "groupName": "分组_1"},
            "valueType": {"type": "enum",
                          "elements": [{"value": "1", "text": "开"},
                                       {"value": "0", "text": "关"}]},
        })
    inputs = []
    for i in range(1, 9):
        inputs.append({
            "id": f"r{i}",
            "name": f"开关{i}",
            "expands": {"required": False},
            "valueType": {"type": "enum",
                          "elements": [{"value": "1", "text": "开"},
                                       {"value": "0", "text": "关"}]},
        })
    funcs = [{"id": "write", "name": "写继电器", "expands": {},
              "async": True, "inputs": inputs, "output": {}}]
    return json.dumps({"properties": props, "functions": funcs},
                      ensure_ascii=False, indent=None)


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
    except Exception as e:
        return -1, {"err": str(e)}


def main():
    code, r = req("POST", "/authorize/login",
                  {"username": "admin5", "password": "Admin@group5"})
    token = r["result"]["token"]
    print("登录 OK")

    new_product = {
        "id": PRODUCT_ID,
        "name": "8路继电器-lfx",
        "photoUrl": "http://172.16.4.211:9000/assets/device-product.png",
        "classifiedId": "-1-",
        "classifiedName": "智能城市",
        "messageProtocol": MQTT_PROTOCOL_ID,
        "protocolName": "mqtt",
        "transportProtocol": "MQTT",
        "deviceType": "device",
        "metadata": build_metadata(),
        "configuration": {},
        "accessId": MQTT_ACCESS_ID,
        "state": 1,
    }
    code, r = req("POST", "/product", new_product, token)
    print(f"POST /product -> {code}")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:1200])
    if code == 200:
        with open("_created_relay8_product.json", "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print("创建成功，已存 _created_relay8_product.json")


if __name__ == "__main__":
    main()
