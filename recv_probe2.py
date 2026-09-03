# -*- coding: utf-8 -*-
"""对照实验：探测订阅者收到计数 + 网关实时状态
- 探测订阅者 A: 订阅 /mqtt-iot/+/+/properties/report (与网关同款)
- 探测订阅者 B: 订阅 /mqtt-iot/+/properties/report  (单层版)
- 发布 3 条双层、3 条单层，观察 A/B/网关 各自 recv 变化
"""
import json
import time
import urllib.request

import paho.mqtt.client as mqtt

EMQX_HOST = "172.16.4.211"
EMQX_PORT = 9783
MQTT_USER = "test"
MQTT_PASS = "123456"
DASH = "http://172.16.4.211:9183/api/v5"
GW = "jetlinks-g5-lfx"
DEV = "FILE-TERM-01"

recv_log = {}


def dash_get(path, token):
    r = urllib.request.Request(DASH + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(r, timeout=15) as x:
        return json.loads(x.read().decode())


def main():
    tok = json.load(urllib.request.urlopen(
        urllib.request.Request(DASH + "/login",
                               data=json.dumps({"username": "group5", "password": "Admin@group5"}).encode(),
                               headers={"Content-Type": "application/json"}), timeout=15))["token"]

    def gw_state():
        d = dash_get(f"/clients/{GW}", tok)
        return d.get("connected"), d.get("connected_at"), d.get("recv_msg"), d.get("recv_cnt"), d.get("mountpoint")

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="ctl-probe-A")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(0.3)
    c.subscribe("/mqtt-iot/+/+/properties/report", qos=1)
    c.subscribe("/mqtt-iot/+/properties/report", qos=1)
    time.sleep(0.5)
    c.on_message = lambda cl, ud, m: recv_log.setdefault(m.topic, []).append(m.payload.decode())

    def pub(topic, n=3):
        for i in range(n):
            c.publish(topic, json.dumps({
                "deviceId": DEV,
                "properties": {"temperature": 20 + i * 0.1, "humidity": 55},
            }), qos=1)
        time.sleep(1.2)

    print("=" * 62)
    print("网关实时状态:", gw_state())

    print("发布 3 条双层...")
    pub(f"/mqtt-iot/mqtt-iot/{DEV}/properties/report")
    print("  探测订阅者收到:", {k: len(v) for k, v in recv_log.items()})
    print("  网关状态:", gw_state())

    print("发布 3 条单层...")
    pub(f"/mqtt-iot/{DEV}/properties/report")
    print("  探测订阅者收到:", {k: len(v) for k, v in recv_log.items()})
    print("  网关状态:", gw_state())

    print("=" * 62)
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
