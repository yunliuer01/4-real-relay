# -*- coding: utf-8 -*-
"""8 路继电器-lfx 端到端闭环验证（新物模型：34 属性 + switch_change 事件 + 功能）

验证点：
  1. JetLinks 设备 RELAY8-TERM-01 在线（模拟器持续上报）
  2. EMQX 三条规则 rule_lfx_relay8_{property,reply,cmd} 存在、命中增长且无 failed
  3. 上行：属性上报 -> /properties/report 载荷含 34 个新物模型属性
     (ch1..8 状态/电压/电流/功率 + 环境温度/湿度)
  4. 事件：状态变化 -> /event/switch_change {channel,state}
  5. 下行(经 JetLinks 真实 REST 调用)：
     switch_all(true) -> 全部开启；set_channel(3,false) -> 第3路关闭
     -> 模拟器收到 /service/cmd 并回复 /function/invoke/reply
       {messageId, success:true, output:true}
  6. 属性读/写直连主题（properties/read、properties/write）回复可用
  7. 结束时 switch_all(false) 把 8 路全部恢复为关闭

前置：设备模拟器已启动（python main.py）
运行：python verify/e2e_verify.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

JL = config.JETLINKS_API
EMQX = config.EMQX_API
DEVICE = config.DEVICE_ID
PRODUCT = config.PRODUCT_ID
MQTT_HOST, MQTT_PORT = config.MQTT_HOST, config.MQTT_PORT

EXPECT_KEYS = (["ch%d_%s" % (c, k)
                for c in range(1, 9)
                for k in ("state", "voltage", "current", "power")]
               + ["temperature", "humidity"])


def req(url, headers=None, data=None, method=None, timeout=30):
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
        subs = [(f"/{PRODUCT}/{DEVICE}/properties/report", 1),
                (f"/{PRODUCT}/{DEVICE}/function/invoke/reply", 1),
                (f"/{PRODUCT}/{DEVICE}/event/switch_change", 1),
                (f"/{PRODUCT}/{DEVICE}/properties/read/reply", 1),
                (f"/{PRODUCT}/{DEVICE}/properties/write/reply", 1),
                (f"/{PRODUCT}/+/service/cmd", 1)]
        c.subscribe(subs)
        print("MQTT 已连接并订阅 properties/report / function/invoke/reply /\n"
              "         event/switch_change / properties read|write reply / +/service/cmd")


def on_message(c, u, msg):
    try:
        p = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        p = msg.payload
    with lock:
        received.append((msg.topic, p, time.time()))
    print("  <<< [%s] %s" % (msg.topic, json.dumps(p, ensure_ascii=False)[:240]))


def find_in(topic_pred, body_pred=None, timeout=8.0):
    """轮询历史+等待新消息，返回 (topic,payload) 或 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            for t, p, _ in received:
                if topic_pred(t) and (body_pred is None or body_pred(p)):
                    return (t, p)
        time.sleep(0.3)
    return None


def wait_report_state(states, timeout=20.0):
    """等一条属性上报与指定 {idx1: bool} 一致"""
    def pred(p):
        props = (p or {}).get("properties") or {}
        return all(props.get("ch%d_state" % i) is states[i] for i in states)
    return find_in(lambda t: t.endswith("/properties/report"), pred, timeout)


def wait_reply_ok(mid=None, timeout=20.0):
    def pred(p):
        if (p or {}).get("success") is not True:
            return False
        if mid is not None and str(p.get("messageId")) != str(mid):
            return False
        return True
    return find_in(lambda t: t.endswith("/function/invoke/reply"), pred, timeout)


def wait_event(channel, state, timeout=20.0):
    def pred(p):
        d = (p or {}).get("data") or {}
        return d.get("channel") == channel and d.get("state") is state
    return find_in(lambda t: t.endswith("/event/switch_change"), pred, timeout)


def rule_metrics(eh):
    st, b = req(EMQX + "/rules?limit=500", eh)
    out = {}
    for r in (b.get("data") or []):
        if (r.get("name") or "").startswith("rule_lfx_relay8"):
            st2, m = req(EMQX + f"/rules/{r['id']}/metrics", eh)
            mm = m.get("metrics") or {}
            out[r["name"]] = {"matched": mm.get("matched", 0),
                              "passed": mm.get("passed", 0),
                              "failed": mm.get("failed", 0),
                              "a_failed": ((mm.get("actions") or {}).get("failed", 0))}
    return out


# ---------------- 主流程 ----------------
def main():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        print("%s  %s  %s" % ("[PASS]" if cond else "[FAIL]", name,
                              "" if cond else detail))
        if cond:
            ok += 1
        else:
            fail += 1

    # 登录
    st, b = req(JL + "/authorize/login", {"Content-Type": "application/json"},
                {"username": config.JETLINKS_WEB_USER,
                 "password": config.JETLINKS_WEB_PASS})
    jtok = (b.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}
    st, b = req(EMQX + "/login", {"Content-Type": "application/json"},
                {"username": config.EMQX_ADMIN_USER,
                 "password": config.EMQX_ADMIN_PASS})
    etok = b.get("token")
    eh = {"Authorization": "Bearer " + (etok or "")}
    print("登录 JetLinks / EMQX OK")

    # 设备在线
    st, b = req(JL + "/device/instance/_query/no-paging", jh,
                {"terms": [{"column": "id", "termType": "eq", "value": DEVICE}]})
    res = b.get("result")
    dev = (res or [{}])[0]
    st_txt = (dev.get("state") or {}).get("text")
    check("JetLinks 设备在线", st_txt == "在线", "实际: %s" % st_txt)

    # EMQX 规则
    m0 = rule_metrics(eh)
    check("EMQX 存在 3 条本组规则", {"cmd", "property", "reply"}
          <= {n.split("relay8_")[-1] for n in m0}, str(list(m0)))

    # MQTT 监听
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="relay-e2e-" + uuid.uuid4().hex[:6],
                         clean_session=True)
    client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    time.sleep(2)

    # 3. 上行：34 属性完整上报
    hit = find_in(lambda t: t.endswith("/properties/report"),
                  lambda p: isinstance(p.get("properties"), dict), 15)
    if hit:
        props = hit[1]["properties"]
        missing = [k for k in EXPECT_KEYS if k not in props]
        check("属性上报含 34 个新属性", not missing, "缺失: %s" % missing[:8])
    else:
        check("属性上报含 34 个新属性", False, "15s 未收到上报")

    # 5. 平台真实调用 switch_all(true)
    print("-> 调用 switch_all(true) ...")
    st, b = req(JL + f"/device/instance/{DEVICE}/function/switch_all", jh,
                {"state": True}, method="POST")
    print("   REST:", st, json.dumps(b, ensure_ascii=False)[:200])
    r = wait_reply_ok(timeout=40)
    check("switch_all 收到平台式回复(success/output)",
          r is not None, "40s 无 function/invoke/reply")
    st_map = {i: True for i in range(1, 9)}
    r = wait_report_state(st_map, timeout=25)
    check("switch_all(true) 后 8 路上报全开", r is not None)
    if r:
        ch_states = {int(k[2:-6]): v for k, v in r[1]["properties"].items()
                     if k.endswith("_state")}
        on_els = all(r[1]["properties"].get(f"ch{i}_voltage", 0) > 0 for i in range(1, 9))
        check("开启通道电压>0", on_els)
        check("上报含温湿度", {"temperature", "humidity"} <= set(r[1]["properties"]),
              str(list(r[1]["properties"])[-6:]))

    # 4. 事件：switch_all 产生 8 条
    chans = set()
    deadline = time.time() + 25
    while time.time() < deadline:
        with lock:
            for t, p, _ in received:
                if t.endswith("/event/switch_change") and isinstance(p, dict):
                    chans.add((p.get("data") or {}).get("channel"))
        if len(chans) >= 8:
            break
        time.sleep(0.5)
    check("switch_all 触发 8 路 switch_change 事件", len(chans) >= 8,
          "已收通道: %s" % sorted(chans))

    # 5. 单路关闭 set_channel(3,false)
    print("-> 调用 set_channel(channel=3,state=false) ...")
    st, b = req(JL + f"/device/instance/{DEVICE}/function/set_channel", jh,
                {"channel": 3, "state": False}, method="POST")
    print("   REST:", st, json.dumps(b, ensure_ascii=False)[:200])
    r = wait_reply_ok(timeout=40)
    check("set_channel 收到平台式回复", r is not None)
    r = wait_report_state({1: True, 3: False}, timeout=25)
    check("set_channel(3,false) 后 ch3 关闭(其余保持开)", r is not None)
    if r:
        ch3_el = r[1]["properties"].get("ch3_voltage", -1)
        ch1_el = r[1]["properties"].get("ch1_voltage", 0)
        check("ch3 电压为0 且 ch1 电压>0", ch3_el == 0 and ch1_el > 0,
              "ch3=%s ch1=%s" % (ch3_el, ch1_el))
    r_ev = wait_event(3, False, timeout=15)
    check("set_channel 触发 ch3 关闭事件", r_ev is not None)

    # 6. 属性读：设备侧直读当前属性
    mid_r = "read-" + uuid.uuid4().hex[:6]
    client.publish(f"/{PRODUCT}/{DEVICE}/properties/read",
                   json.dumps({"messageId": mid_r,
                               "properties": ["ch1_state", "temperature"]}), qos=1)
    r = find_in(lambda t: t.endswith("/properties/read/reply"),
                lambda p: str(p.get("messageId")) == mid_r
                and isinstance(p.get("properties"), dict), 15)
    check("属性读取收到 read/reply",
          r is not None and {"ch1_state", "temperature"} <= set(r[1]["properties"]),
          json.dumps(r[1] if r else {}, ensure_ascii=False)[:200])

    # 6. 属性写：设备侧写 ch7_state=false 再恢复
    mid_w = "write-" + uuid.uuid4().hex[:6]
    client.publish(f"/{PRODUCT}/{DEVICE}/properties/write",
                   json.dumps({"messageId": mid_w,
                               "properties": {"ch7_state": False}}), qos=1)
    r = find_in(lambda t: t.endswith("/properties/write/reply"),
                lambda p: str(p.get("messageId")) == mid_w, 15)
    check("属性写入收到 write/reply(success)",
          r is not None and r[1].get("success") is True,
          json.dumps(r[1] if r else {}, ensure_ascii=False)[:200])
    r = wait_report_state({1: True, 7: False}, timeout=15)
    check("写属性 ch7_state=false 生效", r is not None)

    # 7. 收尾：switch_all(false) 全关，保持干净状态
    st, b = req(JL + f"/device/instance/{DEVICE}/function/switch_all", jh,
                {"state": False}, method="POST")
    r = wait_reply_ok(timeout=40)
    check("收尾 switch_all(false) 回复 OK", r is not None)
    r = wait_report_state({i: False for i in range(1, 9)}, timeout=25)
    check("8 路最终全部关闭", r is not None)

    client.loop_stop()
    client.disconnect()

    # 规则增量
    m1 = rule_metrics(eh)
    print("[EMQX 规则增量]")
    all_pass = True
    for n in m0:
        d = {k: m1[n][k] - m0[n][k] for k in m0[n]}
        print("   %-28s %s" % (n, d))
        if d["matched"] <= 0 or d["failed"] > 0 or d["a_failed"] > 0:
            all_pass = False
    check("EMQX 规则命中增长且无失败", all_pass)

    print("=" * 66)
    print("结果: PASS=%d FAIL=%d" % (ok, fail))
    print(f"请到 JetLinks: 产品 {PRODUCT} -> 设备 {DEVICE} -> 运行状态 核对"
          "（8路状态/电压/电流/功率、温度、湿度、switch_change 事件、功能调用记录）")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
