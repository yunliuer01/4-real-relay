# -*- coding: utf-8 -*-
"""JetLinks 平台资源准备：创建 8 路继电器产品 relay8_lfx（幂等：已存在则跳过）

物模型（与 demo relay_8ch_product 对齐，见 relay8_thing_model.py）：
  34 属性（8路 状态/电压/电流/功率 + 环境温度/湿度）
  + switch_change 事件 + set_channel/switch_all 功能
接入方式：mqtt 接入网关（与已完成小组 relay4_mt 一致）

运行：python setup/create_product.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay8_thing_model import build_metadata, metadata_summary  # noqa: E402

BASE = config.JETLINKS_API
PRODUCT_ID = config.PRODUCT_ID
MQTT_PROTOCOL_ID = config.MQTT_PROTOCOL_ID     # mqtt 协议（平台内置 demo）
MQTT_ACCESS_ID = config.MQTT_ACCESS_ID         # mqtt接入 网关（平台内置 demo）


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
                  {"username": config.JETLINKS_WEB_USER,
                   "password": config.JETLINKS_WEB_PASS})
    token = r["result"]["token"]
    print("登录 OK")

    # 已存在则跳过（产品物模型如需升级走 upgrade_product_model.py）
    code, r = req("GET", f"/device/product/{PRODUCT_ID}", None, token)
    if code == 200:
        md = (r.get("result") or {}).get("metadata") or ""
        print(f"产品已存在: metadata {len(md)} 字符 -> 跳过创建")
        return

    metadata = build_metadata()
    np_, ne, nf = metadata_summary(metadata)
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
        "metadata": metadata,
        "configuration": {},
        "accessId": MQTT_ACCESS_ID,
        "state": 1,
    }
    print(f"物模型: 属性={np_} 事件={ne} 功能={nf}")
    code, r = req("POST", "/product", new_product, token)
    print(f"POST /product -> {code}")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:1200])
    if code == 200:
        out = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "_created_relay8_product.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(f"创建成功，已存 {out}")


if __name__ == "__main__":
    main()
