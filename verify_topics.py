# -*- coding: utf-8 -*-
"""验证 JetLinks g5 网关到底认哪种主题格式（只操作本组设备，测试后恢复 online）

实验逻辑：
  A. 对 FILE-TERM-01 发 双层 offline /online  -> 看状态是否翻转（证明双层被解析）
  B. 对 MODBUS-TERM-01 发 单层 online/offline -> 看状态是否变化（对照组）

结果会打印每个步骤后 JetLinks 查询到的设备状态。
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
GW_PREFIX = "/mqtt-iot"      # 网络组件 g5_mqtt_client 的 topicPrefix
PRODUCT = "mqtt-iot"


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


def device_state(token, device_id):
    d = jl_req("POST", "/device/instance/_query/no-paging",
               {"paging": False, "terms": [{"column": "id", "value": device_id}]},
               token=token)
    items = (d.get("result") or []) if isinstance(d, dict) else d
    if isinstance(items, list) and items:
        return (items[0].get("state") or {}).get("value")
    return None


def main():
    token = jl_req("POST", "/authorize/login",
                   {"username": JL_USER, "password": JL_PASS})["result"]["token"]

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id="topic-probe-tmp")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(0.5)

    def pub(topic, payload):
        info = c.publish(topic, json.dumps(payload), qos=1)
        time.sleep(0.8)
        return info.rc

    print("=" * 62)
    for dev in ("FILE-TERM-01", "MODBUS-TERM-01"):
        st0 = device_state(token, dev)
        print(f"[{dev}] 初始状态 = {st0}")

        # A) 双层主题 offline/online
        t = f"{GW_PREFIX}/{PRODUCT}/{dev}/offline"
        pub(t, {"deviceId": dev})
        st1 = device_state(token, dev)
        print(f"  双层offline -> {t}")
        print(f"     状态: {st0} -> {st1}   {'✓ 被解析' if st1 == 'offline' else '✗ 未变化'}")

        t = f"{GW_PREFIX}/{PRODUCT}/{dev}/online"
        pub(t, {"deviceId": dev})
        st2 = device_state(token, dev)
        print(f"  双层online  -> {t}")
        print(f"     状态: {st1} -> {st2}   {'✓ 被解析' if st2 == 'online' else '✗ 未变化'}")

    # B) 对照组：单层 online（模拟当前终端代码发出的格式）
    dev = "FILE-TERM-01"
    st_before = device_state(token, dev)
    t = f"/{PRODUCT}/{dev}/offline"
    pub(t, {"deviceId": dev})
    st_after = device_state(token, dev)
    print(f"[对照组-单层] {dev}  {t}")
    print(f"     状态: {st_before} -> {st_after}   {'✗ 被解析(说明单层也通!)' if st_after != st_before else '✓ 未变化(单层不被解析)'}")

    # 确保恢复 online（用被解析的双层格式）
    t = f"{GW_PREFIX}/{PRODUCT}/{dev}/online"
    pub(t, {"deviceId": dev})
    st_final = device_state(token, dev)
    print(f"[恢复] {dev} 双层online -> {t}  最终状态={st_final}")

    c.loop_stop()
    c.disconnect()
    print("=" * 62)


if __name__ == "__main__":
    main()
