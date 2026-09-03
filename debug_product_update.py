# -*- coding: utf-8 -*-
"""分步调试：先测 metadata 更新，再测 accessId 网关绑定，打印错误详情"""
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
        return "OK " + json.dumps(r, ensure_ascii=False)[:250]
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}"
    except Exception as e:
        return f"ERR {e}"


def main():
    token = req("POST", "/authorize/login",
                {"username": "admin5", "password": "Admin@group5"})["result"]["token"]

    p = req("POST", "/device/product/_query/no-paging",
            {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
            token=token)["result"][0]
    print("当前 accessId:", p.get("accessId"), " accessName:", p.get("accessName"),
          " state:", p.get("state"))
    meta = json.loads(p["metadata"])
    print("当前功能:", [f["id"] for f in meta.get("functions", [])])

    # ---- 步骤1：只更新 metadata（补 setTH），其余字段不动 ----
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
        p2 = dict(p)
        p2["metadata"] = json.dumps(meta, ensure_ascii=False)
        print("\n[步骤1] 仅更新 metadata ->", try_req("PUT", "/device/product/mqtt-iot", p2, token))
    else:
        print("\n[步骤1] setTH 已存在，跳过")

    # ---- 步骤2：只更新 accessId（绑定新网关） ----
    p3 = dict(p)
    p3["accessId"] = NEW_GATEWAY
    p3["accessName"] = "g5-mqtt-gateway"
    p3["accessProvider"] = "mqtt-client-gateway"
    print("[步骤2] 仅更新 accessId ->", try_req("PUT", "/device/product/mqtt-iot", p3, token))

    # ---- 步骤3：尝试 POST /device/product/{id}/_deploy（部分版本改配置后需重新发布） ----
    print("[步骤3] 重新发布产品 ->", try_req("POST", "/device/product/mqtt-iot/_deploy", {}, token))

    # ---- 确认最终状态 ----
    try:
        pf = req("POST", "/device/product/_query/no-paging",
                 {"paging": False, "terms": [{"column": "id", "value": "mqtt-iot"}]},
                 token=token)["result"][0]
        print("\n最终 accessId:", pf.get("accessId"), " accessName:", pf.get("accessName"))
        mf = json.loads(pf["metadata"])
        print("最终功能:", [f["id"] for f in mf.get("functions", [])])
    except Exception as e:
        print("确认失败:", e)


if __name__ == "__main__":
    main()
