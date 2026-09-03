# -*- coding: utf-8 -*-
"""收包计数实验：确定 JetLinks 网关实际收到的 properties/report 格式
- 先发 10 条单层 /mqtt-iot/{dev}/properties/report
- 再发 10 条双层 /mqtt-iot/mqtt-iot/{dev}/properties/report
观察网关客户端 jetlinks-g5-lfx 的 recv_msg 增量。只操作本组设备。
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


def dash_get(path, token):
    r = urllib.request.Request(DASH + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(r, timeout=15) as x:
        return json.loads(x.read().decode())


def main():
    tok = json.load(urllib.request.urlopen(
        urllib.request.Request(DASH + "/login",
                               data=json.dumps({"username": "group5", "password": "Admin@group5"}).encode(),
                               headers={"Content-Type": "application/json"}), timeout=15))["token"]

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id="recv-probe-tmp")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(0.5)

    def gw_recv():
        d = dash_get(f"/clients/{GW}", tok)
        return d.get("recv_msg")

    def burst(topic, n=10):
        for i in range(n):
            c.publish(topic, json.dumps({
                "deviceId": DEV,
                "properties": {"temperature": 20 + i * 0.1, "humidity": 55 + i * 0.1},
            }), qos=1)
        time.sleep(1.5)

    print("=" * 60)
    r0 = gw_recv()
    print(f"基线 recv_msg = {r0}")

    burst(f"/mqtt-iot/{DEV}/properties/report")
    r1 = gw_recv()
    print(f"发完 10 条【单层】后 recv_msg = {r1}  增量 = {r1 - r0}")

    burst(f"/mqtt-iot/mqtt-iot/{DEV}/properties/report")
    r2 = gw_recv()
    print(f"发完 10 条【双层】后 recv_msg = {r2}  增量 = {r2 - r1}")

    print("=" * 60)
    if r2 - r1 > 0 and r1 - r0 == 0:
        print("结论: 只有【双层】主题被网关接收 → EMQX规则/终端 需改用双层主题")
    elif r1 - r0 > 0 and r2 - r1 == 0:
        print("结论: 只有【单层】主题被网关接收 → 保持现状")
    elif r1 - r0 > 0 and r2 - r1 > 0:
        print("结论: 单层、双层都被接收")
    else:
        print("结论: 两者都没被接收?!")

    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
