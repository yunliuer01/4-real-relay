# -*- coding: utf-8 -*-
"""EMQX 规则准备：为 relay8_lfx / RELAY8-TERM-01 维护 3 条转发规则（幂等 upsert）

链路（与 core/simulator.py 配套）：
  1. rule_lfx_relay8_property：设备 --property/post--> properties/report (JetLinks 入库)
  2. rule_lfx_relay8_reply   ：设备 --function/post--> function/invoke/reply (JetLinks 收响应)
  3. rule_lfx_relay8_cmd     ：JetLinks --function/invoke--> service/cmd (设备收命令)
注意：switch_change 事件由模拟器直发 /event/switch_change（网关通配订阅 /event/+，
      无需、也不宜经 EMQX 规则转发），故无事件规则；历史遗留的 rule_lfx_relay8_event
      会被自动删除。

规则统一"原始透传"(SELECT payload)：报文统一由模拟器在 Python 侧按 JetLinks 规范化
格式拼装/解析，避免 EMQX jq 子集(不支持 select/tonumber?/first() 等运算，已实测)造成
failed.exception。规则按 name 幂等 upsert：已存在则原位更新，否则新建；重复运行安全。

运行：python setup/create_rules.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

BASE = config.EMQX_API
DEVICE = config.DEVICE_ID
PRODUCT = config.PRODUCT_ID


def _rule(rid, name, desc, from_topic, to_topic):
    return {
        "id": rid,
        "name": name,
        "description": desc,
        "sql": f'SELECT payload AS new_payload FROM "{from_topic}"',
        "from": from_topic,
        "to": to_topic,
    }


RULES = [
    _rule(None, "rule_lfx_relay8_property",
          "8路继电器-lfx 属性上报 -> JetLinks",
          f"/{PRODUCT}/+/property/post",
          f"/{PRODUCT}/{DEVICE}/properties/report"),
    _rule(None, "rule_lfx_relay8_reply",
          "8路继电器-lfx 功能响应 -> JetLinks",
          f"/{PRODUCT}/+/function/post",
          f"/{PRODUCT}/{DEVICE}/function/invoke/reply"),
    _rule(None, "rule_lfx_relay8_cmd",
          "JetLinks 功能下发 -> 8路继电器-lfx 命令(原始透传)",
          f"/{PRODUCT}/+/function/invoke",
          f"/{PRODUCT}/{DEVICE}/service/cmd"),
]

# 事件不走 EMQX 规则（直发），如残留旧版事件规则则清理
STALE_EVENT_RULE = "rule_lfx_relay8_event"


def req(method, url, body=None, token=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"exc": str(e)[:100]}


def main():
    _, r = req("POST", BASE + "/login",
               {"username": config.EMQX_ADMIN_USER,
                "password": config.EMQX_ADMIN_PASS})
    token = r.get("token")
    print("EMQX 登录 OK")

    st, b = req("GET", BASE + "/rules?limit=500", token=token)
    existing = {}
    for it in (b.get("data") or (b if isinstance(b, list) else [])):
        existing[it.get("name")] = it
    print("现有规则数:", len(existing))

    for rule in RULES:
        body = {
            "name": rule["name"],
            "sql": rule["sql"],
            "actions": [{
                "function": "republish",
                "args": {"topic": rule["to"], "payload": "${new_payload}",
                         "qos": 1, "retain": False},
            }],
            "description": rule["description"],
        }
        if rule["name"] in existing:
            rid = existing[rule["name"]]["id"]
            code, r = req("PUT", BASE + f"/rules/{rid}", body, token)
            print(f"UPDATE {rule['name']} (id={rid}) -> {code}")
        else:
            code, r = req("POST", BASE + "/rules", body, token)
            rid = (r or {}).get("id", "")
            print(f"CREATE {rule['name']} (id={rid}) -> {code}")
        if code not in (200, 201):
            print("  FAIL:", json.dumps(r, ensure_ascii=False)[:300])
        else:
            print("  SQL:", rule["sql"])
            print("  republish ->", rule["to"])

    if STALE_EVENT_RULE in existing:
        rid = existing[STALE_EVENT_RULE]["id"]
        code, _ = req("DELETE", BASE + f"/rules/{rid}", token=token)
        print(f"DELETE 旧事件规则 {STALE_EVENT_RULE} (id={rid}) -> {code}")


if __name__ == "__main__":
    main()
