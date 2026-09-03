# -*- coding: utf-8 -*-
"""验证2：加长等待版 —— 逐条发送、确认翻转后再发下一条
只操作本组设备 FILE-TERM-01 / MODBUS-TERM-01，结束后恢复 online。
"""
import json
import time
import urllib.request
import urllib.error

import paho.mqtt.client as mqtt

EMQX_HOST = "172.16.4.211"
EMQX_PORT = 9783
MQTT_USER = "test"
MQTT_PASS = "123456"
JETLINKS = "http://172.16.4.211:9000/api"
JL_USER = "admin5"
JL_PASS = "Admin@group5"
GW_PREFIX = "/mqtt-iot"
PRODUCT = "mqtt-iot"
WAIT = 3.0


def jl_req(method, path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(JETLINKS + path, data=body,
                               headers={"Content-Type": "application/json"})
    r.get_method = lambda: method
    if token:
        r.add_header("X-Access-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return json.loads(x.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"err": f"HTTP {e.code}: {e.read(200).decode('utf-8', 'replace')}"}


def state(token, device_id):
    d = jl_req("POST", "/device/instance/_query/no-paging",
               {"paging": False, "terms": [{"column": "id", "value": device_id}]},
               token=token)
    items = (d.get("result") or []) if isinstance(d, dict) else d
    if isinstance(items, list) and items:
        return (items[0].get("state") or {}).get("value")
    return None


def wait_state(token, dev, expect, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = state(token, dev)
        if s == expect:
            return s, True
        time.sleep(1)
    return s, False


def main():
    token = jl_req("POST", "/authorize/login",
                   {"username": JL_USER, "password": JL_PASS})["result"]["token"]
    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id="topic-probe-tmp2")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(0.5)

    def pub(topic, payload):
        c.publish(topic, json.dumps(payload), qos=1)

    def phase(name, dev, topic, expect):
        pub(topic, {"deviceId": dev})
        s, ok = wait_state(token, dev, expect)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name} -> {topic}")
        print(f"        期望 {expect}，实际 {s}")
        return ok

    print("=" * 62)
    print("实验1：双层主题是否被解析 (FILE-TERM-01)")
    s0 = state(token, "FILE-TERM-01")
    print(f"  当前状态: {s0}")
    p1 = phase("双层 online", "FILE-TERM-01",
               f"{GW_PREFIX}/{PRODUCT}/FILE-TERM-01/online", "online")
    p2 = phase("双层 offline", "FILE-TERM-01",
               f"{GW_PREFIX}/{PRODUCT}/FILE-TERM-01/offline", "offline")
    p3 = phase("双层 online(恢复)", "FILE-TERM-01",
               f"{GW_PREFIX}/{PRODUCT}/FILE-TERM-01/online", "online")

    print("-" * 62)
    print("实验2：单层主题是否被解析 (MODBUS-TERM-01, 对照组)")
    s0 = state(token, "MODBUS-TERM-01")
    print(f"  当前状态: {s0}")
    p4 = phase("单层 offline", "MODBUS-TERM-01",
               f"/{PRODUCT}/MODBUS-TERM-01/offline", "offline")
    p5 = phase("双层 online(恢复)", "MODBUS-TERM-01",
               f"{GW_PREFIX}/{PRODUCT}/MODBUS-TERM-01/online", "online")

    print("=" * 62)
    print("结论：", "双层被解析" if (p1 and p2) else "双层解析异常",
          "|", "单层也被解析!" if p4 else "单层不被解析(符合预期)")
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
