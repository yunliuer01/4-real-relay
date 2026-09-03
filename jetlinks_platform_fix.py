# -*- coding: utf-8 -*-
"""把产品 mqtt-iot 绑定到本组网关 g5-mqtt-gateway，并在物模型中补充 setTH 功能"""
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


def main():
    token = req("POST", "/authorize/login",
                {"username": "admin5", "password": "Admin@group5"})["result"]["token"]

    prods = req("POST", "/device/product/_query/no-paging",
                {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
                token=token)["result"]
    p = prods[0]

    # 1. 绑定到本组新网关
    p["accessId"] = NEW_GATEWAY
    p["accessName"] = "g5-mqtt-gateway"
    p["accessProvider"] = "mqtt-client-gateway"

    # 2. 物模型增加 setTH 功能（平台功能按钮来自这里）
    meta = json.loads(p["metadata"])
    if not any(f.get("id") == "setTH" for f in meta.get("functions", [])):
        meta.setdefault("functions", []).append({
            "id": "setTH",
            "name": "设置温湿度",
            "async": False,
            "inputs": [
                {"id": "temperature", "name": "温度(℃)",
                 "expands": {"required": False},
                 "valueType": {"type": "float", "max": 80, "min": -40, "step": 0.1}},
                {"id": "humidity", "name": "湿度(%RH)",
                 "expands": {"required": False},
                 "valueType": {"type": "float", "max": 100, "min": 0, "step": 0.1}},
            ],
            "output": {"type": "string", "textType": "string"},
            "description": "远程设置温湿度（可只填一项，另一项保持不变）",
        })
    p["metadata"] = json.dumps(meta, ensure_ascii=False)

    r = req("PUT", "/device/product/mqtt-iot", p, token=token)
    print("保存产品:", json.dumps(r, ensure_ascii=False)[:150])

    # 确认
    p2 = req("POST", "/device/product/_query/no-paging",
             {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
             token=token)["result"][0]
    print("accessId:", p2.get("accessId"), " accessName:", p2.get("accessName"))
    meta2 = json.loads(p2["metadata"])
    print("物模型功能:", [f["id"] for f in meta2.get("functions", [])])
    print("物模型属性:", [x["id"] for x in meta2.get("properties", [])])


if __name__ == "__main__":
    main()
