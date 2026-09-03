# -*- coding: utf-8 -*-
"""Day3/4 任务：为 relay8_lfx / RELAY8-TERM-01 创建 EMQX 规则（参照已完成小组 relay4_mt 三条规则）

链路：
  设备 --property/post--> EMQX 规则A -> /relay8_lfx/RELAY8-TERM-01/properties/report (JetLinks 入库)
  设备 --function/post--> EMQX 规则B -> /relay8_lfx/RELAY8-TERM-01/function/invoke/reply (JetLinks 收响应)
  JetLinks --/relay8_lfx/RELAY8-TERM-01/function/invoke--> EMQX 规则C -> /relay8_lfx/RELAY8-TERM-01/service/cmd (设备收命令)

注意：规则C 采用"原始透传"（SELECT payload，不改报文），由模拟器在 Python 侧解析
JetLinks 原始报文。不要改成 jq 拆解 inputs —— 本环境 EMQX 的 jq 子集不支持
select/tonumber?/first()/// 等运算，命中后必然 failed.exception（已实测验证）。
"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9183/api/v5"
DEVICE = "RELAY8-TERM-01"
PRODUCT = "relay8_lfx"


def _jq_props_expr():
    """把 payload.ch{i}_state(bool) 映射为 properties.r{i}(1/0)，生成 jq 表达式片段"""
    parts = []
    for i in range(1, 9):
        parts.append(f"r{i}: (if .ch{i}_state then 1 else 0 end)")
    return ", ".join(parts)


RULES = [
    {
        "name": "rule_lfx_relay8_property",
        "description": "8路继电器-lfx 属性上报->JetLinks",
        "sql": (
            "SELECT first(jq('{productId: \"" + PRODUCT + "\", deviceId: .deviceId, "
            "timestamp: .timestamp, properties: {" + _jq_props_expr() + "}}', payload)) "
            "AS new_payload FROM \"/" + PRODUCT + "/+/property/post\""
        ),
        "topic": f"/{PRODUCT}/{DEVICE}/properties/report",
    },
    {
        "name": "rule_lfx_relay8_reply",
        "description": "8路继电器-lfx 功能响应->JetLinks",
        "sql": (
            "SELECT first(jq('{productId: \"" + PRODUCT + "\", deviceId: .deviceId, id: .id, "
            "success: .success, method: .method, message: .message, data: .data}', payload)) "
            "AS new_payload FROM \"/" + PRODUCT + "/+/function/post\""
        ),
        "topic": f"/{PRODUCT}/{DEVICE}/function/invoke/reply",
    },
    {
        "name": "rule_lfx_relay8_cmd",
        "description": "JetLinks 功能下发->8路继电器-lfx 命令(原始透传)",
        "sql": (
            "SELECT payload AS new_payload FROM \"/" + PRODUCT + "/+/function/invoke\""
        ),
        "topic": f"/{PRODUCT}/{DEVICE}/service/cmd",
    },
]


def req(method, url, body=None, token=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    _, r = req("POST", BASE + "/login",
               {"username": "group5", "password": "Admin@group5"})
    token = r.get("token")
    print("EMQX 登录 OK")

    # 查重：同 ID 已存在则跳过（EMQX 规则 ID 自动生成，用 name 判断存在性不可靠，这里直接创建并记录返回）
    for rule in RULES:
        body = {
            "name": rule["name"],
            "sql": rule["sql"],
            "actions": [{
                "function": "republish",
                "args": {"topic": rule["topic"], "payload": "${new_payload}",
                         "qos": 1, "retain": False},
            }],
            "description": rule["description"],
        }
        code, r = req("POST", BASE + "/rules", body, token)
        if code in (200, 201):
            rid = r.get("id", r.get("result", {}).get("id", ""))
            print(f"✅ {rule['name']} 创建成功 id={rid}")
            print(f"   SQL: {rule['sql']}")
            print(f"   -> republish {rule['topic']}")
        else:
            msg = json.dumps(r, ensure_ascii=False)[:200]
            print(f"❌ {rule['name']} 失败 code={code}: {msg}")


if __name__ == "__main__":
    main()
