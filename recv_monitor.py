# -*- coding: utf-8 -*-
"""连续监控：每 1s 读一次网关客户端计数器，观察发布双层消息后各字段何时变化"""
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
        return (d.get("recv_cnt"), d.get("recv_msg"), d.get("send_msg"),
                d.get("mqueue_len"), d.get("mailbox_len"))

    stop = threading.Event()

    def monitor():
        t0 = time.time()
        prev = snap()
        print("t=%.1f 基线 recv_cnt=%s recv_msg=%s send_msg=%s mq=%s mb=%s" % ((0.0,) + prev))
        last = prev
        while not stop.is_set():
            time.sleep(1.0)
            s = snap()
            if s != last:
                print("t=%5.1f recv_cnt=%s recv_msg=%s send_msg=%s mq=%s mb=%s   [Δcnt=%+d]" %
                      ((time.time() - t0) + (s[0] - prev[0],) + s[1:] + (s[0] - last[0],)))
                last = s

    threading.Thread(target=monitor, daemon=True).start()

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="mon-probe")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(EMQX_HOST, EMQX_PORT, 30)
    c.loop_start()
    time.sleep(1)

    print(">> 发布 3 条双层 properties/report ...")
    for i in range(3):
        c.publish(f"/mqtt-iot/mqtt-iot/{DEV}/properties/report", json.dumps({
            "deviceId": DEV,
            "properties": {"temperature": 30.1 + i, "humidity": 60 + i},
        }), qos=1)
    time.sleep(8)
    stop.set()
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
