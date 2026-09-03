# -*- coding: utf-8 -*-
"""把 rule_lfx_relay8_cmd 更新为"原始透传"（本组最终采用的实现）。

背景（踩坑记录）：
  JetLinks --/relay8_lfx/+/function/invoke--> 本规则 -> /relay8_lfx/RELAY8-TERM-01/service/cmd
  最初参考 relay4_mt 用 jq 把 JetLinks inputs 拆成 {id,method,params:{chN_state}}，
  但实测本环境 EMQX 的 jq 子集不支持 select/tonumber?/first()/// 等运算，
  规则命中后全部 failed.exception（failed=1, passed=0）。
  上行属性/回复规则只用"对象构造"类 jq 所以正常。

  结论：命令规则退化为 SELECT payload 纯透传，由模拟器 relay_terminal.py
  在 Python 侧解析 JetLinks 原始报文（messageId/functionId/inputs），
  同时也兼容标准 /service/cmd {id,method,params} 格式。
用 PUT /rules/{id} 原位更新。
"""
import json
import urllib.request
import urllib.error

BASE = "http://172.16.4.211:9183/api/v5"
RULE_ID = "c883a49e"
DEVICE = "RELAY8-TERM-01"
PRODUCT = "relay8_lfx"


def req(url, headers=None, data=None, method=None, timeout=30):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def main():
    st, b = req(BASE + "/login", headers={"Content-Type": "application/json"},
                data={"username": "group5", "password": "Admin@group5"})
    token = b.get("token")
    sql = f'SELECT payload AS new_payload FROM "/{PRODUCT}/+/function/invoke"'
    body = {
        "name": "rule_lfx_relay8_cmd",
        "description": "JetLinks 功能下发->8路继电器-lfx 命令(原始透传,终端侧解析 inputs)",
        "sql": sql,
        "actions": [{
            "function": "republish",
            "args": {"topic": f"/{PRODUCT}/{DEVICE}/service/cmd",
                     "payload": "${new_payload}", "qos": 1, "retain": False},
        }],
    }
    code, r = req(BASE + f"/rules/{RULE_ID}", data=body, method="PUT",
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer " + token})
    print(f"PUT /rules/{RULE_ID} -> {code}")
    if code in (200, 201):
        print("SQL 已更新为纯透传:", sql)
    else:
        print(json.dumps(r, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
