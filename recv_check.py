# -*- coding: utf-8 -*-
"""重连后收包验证2：监控 recv_cnt/recv_msg/mqueue，发 5 条双层、5 条单层"""
import json
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

EMQX_HOST = "172.16.4.211"
EMQX_PORT = 9783
MQTT_USER = "test"
MQTT_PASS = "123456"
DASH = "http://172.16.4.211:9183/api/v5"
GW = "jetlinks-g5-lfx"
DEV = "MODBUS-TERM-01"


def main():
    tok = json.load(urllib.request.urlopen(
        urllib.request.Request(DASH + "/login",
                               data=json.dumps({"username": "group5", "password": "Admin@group5"}).encode(),
                               headers={"Content-Type": "application/json"}), timeout=15))["token"]

    def snap():
        r = urllib.request.Request(DASH + f"/clients/{GW}",
                                   headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(r, timeout=15) as x:
            d = json.loads(x.read().decode())
        return (d.get("connected_at"), d.get("recv_cnt"), d.get("recv_msg"),
                d.get("recv_oct"), d.get("send_msg"), d.get("mailbox_len"))

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="recv-chk")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(1)

    def burst(topic, n):
        print(f">> 发布 {n} 条 {topic}")
        for i in range(n):
            c.publish(topic, json.dumps({"deviceId": DEV,
                                         "properties": {"temperature": 25 + i, "humidity": 50}}), qos=1)
        for t in range(6):
            time.sleep(2)
            s = snap()
            print(f"   t+{2*(t+1):>2}s connected_at={s[0][11:19]} recv_cnt={s[1]} recv_msg={s[2]} recv_oct={s[3]} send_msg={s[4]} mb={s[5]}")

    print("基线:", snap())
    burst(f"/mqtt-iot/mqtt-iot/{DEV}/properties/report", 5)   # 双层
    burst(f"/mqtt-iot/{DEV}/properties/report", 5)            # 单层
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
