# -*- coding: utf-8 -*-
"""Day3/4 8路继电器-lfx 端到端闭环验证（只读 + 一次下行测试命令）

验证点：
  1. JetLinks 设备 RELAY8-TERM-01 已因属性上报上线
  2. EMQX 三条规则 rule_lfx_relay8_{property,reply,cmd} 命中计数在增长
  3. 上行：/relay8_lfx/+/property/post -> EMQX -> /properties/report（r1..r8 1/0）
  4. 下行：向 /function/invoke 发 write（兼容直出与 params 包裹两种 inputs）
     -> 设备收到 /service/cmd -> 执行 -> /function/post -> /function/invoke/reply
运行：python relay_e2e_verify.py
"""
import json
import random
import threading
import time
import urllib.request
import urllib.error
import uuid

import paho.mqtt.client as mqtt

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"
DEVICE = "RELAY8-TERM-01"
PRODUCT = "relay8_lfx"
MQTT_HOST, MQTT_PORT = "172.16.4.211", 9783


def req(url, headers=None, data=None, method=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
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
    except Exception as e:
        return -1, {"exc": str(e)[:100]}


# ---------------- MQTT 监听侧 ----------------
received = []
lock = threading.Lock()


def on_connect(c, u, f, rc, prop=None):
    if rc == 0:
        c.subscribe([(f"/{PRODUCT}/{DEVICE}/properties/report", 1),
                     (f"/{PRODUCT}/{DEVICE}/function/invoke/reply", 1),
                     (f"/{PRODUCT}/+/service/cmd", 1)])
        print("MQTT 已连接并订阅 properties/report / function/invoke/reply / +/service/cmd")


def on_message(c, u, msg):
    try:
        p = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        p = msg.payload
    with lock:
        received.append((msg.topic, p, time.time()))
    print("  <<< [%s] %s" % (msg.topic, json.dumps(p, ensure_ascii=False)[:300]))


def wait_for(pred, timeout=8.0):
    """pred(topic,payload) 为真即返回 (topic,payload)；超时返回 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            for t, p, ts in received:
                if pred(t, p):
                    return (t, p)
        time.sleep(0.3)
    return None


# ---------------- 主流程 ----------------
def main():
    # 登录
    st, b = req(JL + "/authorize/login", {"Content-Type": "application/json"},
                {"username": "admin5", "password": "Admin@group5"})
    jtok = (b.get("result") or {}).get("token")
    print("[1] JetLinks 登录:", st, "token?", bool(jtok))
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}

    st, b = req(EMQX + "/login", {"Content-Type": "application/json"},
                {"username": "group5", "password": "Admin@group5"})
    etok = b.get("token") or (b.get("data") or {}).get("token")
    print("[2] EMQX 登录:", st)
    eh = {"Authorization": "Bearer " + (etok or "")}

    # 规则 ID 对照 + 计数快照
    st, b = req(EMQX + "/rules?limit=200", eh)
    rules = {r.get("name"): r for r in b.get("data", [])}
    mine = {n: r for n, r in rules.items() if n.startswith("rule_lfx_relay8")}
    print("[3] 本组规则:", {n: r.get("id") for n, r in mine.items()})

    def metrics_snapshot():
        snap = {}
        for n, r in mine.items():
            rid = r.get("id")
            st, b = req(EMQX + f"/rules/{rid}/metrics", eh)
            m = b.get("metrics", {}) or {}
            snap[n] = {"matched": m.get("matched", 0),
                       "passed": m.get("passed", 0),
                       "failed": m.get("failed", 0),
                       "actions_total": (m.get("actions", {}) or {}).get("total", 0),
                       "actions_failed": (m.get("actions", {}) or {}).get("failed", 0)}
        return snap

    m0 = metrics_snapshot()
    print("    规则计数(前):", json.dumps(m0, ensure_ascii=False))

    # 设备在线状态
    st, b = req(JL + "/device/instance/_query/no-paging", jh,
                {"terms": [{"column": "id", "termType": "eq", "value": DEVICE}]})
    res = b.get("result") if isinstance(b, dict) else b
    dev = res[0] if isinstance(res, list) and res else None
    if dev:
        st_txt = (dev.get("state") or {}).get("text")
        print("[4] JetLinks 设备 %s 状态=%s" % (dev.get("id"), st_txt))
        if st_txt != "在线":
            print("    WARN: 设备尚未在线，请确认模拟器已启动且规则 796b260a 有命中")
    else:
        print("[4] 设备查询失败", st, json.dumps(b, ensure_ascii=False)[:200])

    # MQTT 监听
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"relay-e2e-{uuid.uuid4().hex[:6]}", clean_session=True)
    client.username_pw_set("test", "123456")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    time.sleep(2)

    # 上行采样：等一条周期上报落到 JetLinks 主题
    print("[5] 等待属性上报 -> JetLinks 主题 ...")
    hit = wait_for(lambda t, p: t.endswith("/properties/report")
                   and isinstance(p, dict) and "r1" in p.get("properties", {}), 10)
    if hit:
        props = hit[1]["properties"]
        print("    PASS 收到上报:", json.dumps(props, ensure_ascii=False))
    else:
        print("    FAIL 未收到属性上报")

    # 下行用例 A：inputs 直出格式 r1/r5 = 1（透传规则下，设备收到的即原始报文）
    print("[6A] 下发 write (inputs直出 r1=1,r5=1)")
    midA = "test-" + uuid.uuid4().hex[:8]
    client.publish(f"/{PRODUCT}/{DEVICE}/function/invoke",
                   json.dumps({"messageId": midA, "deviceId": DEVICE,
                               "functionId": "write",
                               "inputs": [{"name": "r1", "value": 1},
                                          {"name": "r5", "value": 1}]}), qos=1)
    r = wait_for(lambda t, p: t.endswith("/service/cmd")
                 and isinstance(p, dict) and p.get("messageId") == midA, 8)
    print("    设备收到命令:", "PASS" if r else "FAIL", r[1] if r else "")
    r = wait_for(lambda t, p: t.endswith("/function/invoke/reply")
                 and isinstance(p, dict) and p.get("id") == midA, 8)
    print("    收到 invoke/reply:", "PASS" if r else "FAIL",
          json.dumps(r[1], ensure_ascii=False) if r else "")
    r = wait_for(lambda t, p: t.endswith("/properties/report")
                 and isinstance(p, dict)
                 and p.get("properties", {}).get("r1") == 1
                 and p.get("properties", {}).get("r5") == 1, 8)
    print("    控制后属性(r1=1,r5=1):", "PASS" if r else "FAIL")

    # 下行用例 B：params 包裹格式 r3/r7 = 1（inputs=[{name:params,value:{...}}]）
    print("[6B] 下发 write (params包裹 r3=1,r7=1)")
    midB = "test-" + uuid.uuid4().hex[:8]
    client.publish(f"/{PRODUCT}/{DEVICE}/function/invoke",
                   json.dumps({"messageId": midB, "deviceId": DEVICE,
                               "functionId": "write",
                               "inputs": [{"name": "params",
                                           "value": {"r3": 1, "r7": 1}}]}), qos=1)
    r = wait_for(lambda t, p: t.endswith("/service/cmd")
                 and isinstance(p, dict) and p.get("messageId") == midB, 8)
    print("    设备收到命令:", "PASS" if r else "FAIL", r[1] if r else "")
    r = wait_for(lambda t, p: t.endswith("/function/invoke/reply")
                 and isinstance(p, dict) and p.get("id") == midB, 8)
    print("    收到 invoke/reply:", "PASS" if r else "FAIL",
          json.dumps(r[1], ensure_ascii=False) if r else "")
    r = wait_for(lambda t, p: t.endswith("/properties/report")
                 and isinstance(p, dict)
                 and p.get("properties", {}).get("r3") == 1
                 and p.get("properties", {}).get("r7") == 1, 8)
    print("    控制后属性(r3=1,r7=1):", "PASS" if r else "FAIL")

    client.loop_stop()
    client.disconnect()

    m1 = metrics_snapshot()
    print("[7] 规则计数(后):", json.dumps(m1, ensure_ascii=False))
    for n in mine:
        d = {k: m1[n][k] - m0[n][k] for k in m0[n]}
        print("    增量 %-30s %s" % (n, d))
    print("=" * 60)
    print("闭环验证完成：请在 JetLinks 产品 relay8_lfx -> 设备 RELAY8-TERM-01 -> 运行状态 查看 开关1..8")


if __name__ == "__main__":
    main()
