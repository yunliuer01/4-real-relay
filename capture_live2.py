# -*- coding: utf-8 -*-
"""只读实时抓包 v2：两个独立客户端分别订阅 5 段 / 6 段，各自计数互不干扰。"""
import json
import time
import paho.mqtt.client as mqtt

BROKER = "172.16.4.211"
PORT = 9783
SUB5 = "/mqtt-iot/+/properties/report"
SUB6 = "/mqtt-iot/+/+/properties/report"
DURATION = 12

results = {"5段": [], "6段": []}


def make_collector(key):
    def on_msg(client, userdata, msg):
        try:
            pl = json.loads(msg.payload.decode("utf-8", "replace"))
            dev = (pl or {}).get("deviceId", "?")
            props = (pl or {}).get("properties") or {}
            results[key].append((msg.topic, dev, json.dumps(props, ensure_ascii=False)[:60]))
        except Exception:
            results[key].append((msg.topic, "?", "?"))
    return on_msg


def main():
    clients = []
    for key, sub in (("5段", SUB5), ("6段", SUB6)):
        c = mqtt.Client(client_id="diag_live_%s_%d" % (key, int(time.time() * 1000) % 100000),
                        protocol=mqtt.MQTTv311)
        c.username_pw_set("test", "123456")
        c.on_message = make_collector(key)
        c.connect(BROKER, PORT, 30)
        c.subscribe(sub, qos=0)
        clients.append((key, c))
        print("已订阅 %s -> %s" % (key, sub))

    print("监听 %d 秒..." % DURATION)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        for _, c in clients:
            c.loop(timeout=0.5)
    for _, c in clients:
        c.disconnect()

    for key in ("5段", "6段"):
        rows = results[key]
        print("\n== %s 订阅收到 %d 条 ==" % (key, len(rows)))
        seen = {}
        for topic, dev, props in rows:
            seen.setdefault(dev, []).append(props)
        for dev, plist in seen.items():
            print("   %s : %d条  示例 %s" % (dev, len(plist), plist[0] if plist else ""))


if __name__ == "__main__":
    main()
