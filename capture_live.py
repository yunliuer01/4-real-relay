# -*- coding: utf-8 -*-
"""只读实时抓包：订阅 5 段与 6 段两种模式对比，看 FILE/MODBUS 实时上报能否被收到。"""
import json
import time
import paho.mqtt.client as mqtt

BROKER = "172.16.4.211"
PORT = 9783
SUB5 = "/mqtt-iot/+/properties/report"       # 5 段语义: 匹配 /mqtt-iot/{dev}/properties/report
SUB6 = "/mqtt-iot/+/+/properties/report"     # 6 段语义: 终端发的 5 段消息匹配不上
DURATION = 12


def main():
    seen5 = {}
    seen6 = {}
    got6 = []

    def on_msg(client, userdata, msg):
        topic = msg.topic
        try:
            pl = json.loads(msg.payload.decode("utf-8", "replace"))
            if isinstance(pl, dict):
                dev = pl.get("deviceId", "?")
                props = pl.get("properties") or pl.get("property") or {}
                ts = pl.get("timestamp", "")
                summary = json.dumps(props, ensure_ascii=False)[:80]
            else:
                dev, summary = "?", str(pl)[:80]
        except Exception:
            dev, summary = "?", msg.payload.decode("utf-8", "replace")[:80]
        if topic == SUB5:
            seen5.setdefault(dev, []).append((ts, summary))
        else:
            seen6.setdefault(dev, []).append((ts, summary))
            got6.append((topic, summary))

    c = mqtt.Client(client_id="diag_live5_check", protocol=mqtt.MQTTv311)
    c.username_pw_set("test", "123456")
    c.on_message = on_msg
    c.connect(BROKER, PORT, 30)
    c.subscribe([(SUB5, 0), (SUB6, 0)])
    print("已订阅: %s 与 %s" % (SUB5, SUB6))
    print("监听 %d 秒..." % DURATION)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        c.loop(timeout=1.0)
    c.disconnect()

    print("\n== 5段订阅收到 ==")
    for dev, rows in seen5.items():
        print("  %s 共%d条" % (dev, len(rows)))
        for ts, s in rows[:3]:
            print("     ts=%s props=%s" % (ts, s))
    print("\n== 6段订阅收到 ==")
    if seen6:
        for dev, rows in seen6.items():
            print("  %s 共%d条" % (dev, len(rows)))
            for ts, s in rows[:3]:
                print("     ts=%s props=%s" % (ts, s))
    else:
        print("  (0 条 —— 6段模式确实收不到 5 段消息)")


if __name__ == "__main__":
    main()
