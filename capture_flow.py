# -*- coding: utf-8 -*-
"""只读抓包：监听 terminal/+/th(终端原始) 与 /mqtt-iot/#(转换后)，
判断规则是否命中、网关是否收到。仅订阅不发布。"""
import json
import time
import paho.mqtt.client as mqtt

BROKER = "172.16.4.211"
PORT = 9783
USER, PASS = "test", "123456"
DURATION = 25

seen_raw = []
seen_cvt = []


def on_message(client, userdata, msg):
    try:
        p = json.loads(msg.payload.decode("utf-8", "replace"))
    except Exception:
        p = msg.payload.decode("utf-8", "replace")[:80]
    t = msg.topic
    line = "%s  %s" % (t, json.dumps(p, ensure_ascii=False)[:160])
    if t.startswith("/mqtt-iot/"):
        seen_cvt.append((time.time(), t, p))
        print("[转换后]", line)
    else:
        seen_raw.append((time.time(), t, p))
        print("[原始  ]", line)


def main():
    c = mqtt.Client(client_id="diag_capture_5", protocol=mqtt.MQTTv311)
    c.username_pw_set(USER, PASS)
    c.on_message = on_message
    c.connect(BROKER, PORT, 60)
    c.subscribe([("terminal/+/th", 0), ("/mqtt-iot/#", 0)])
    print("已订阅 terminal/+/th 与 /mqtt-iot/#，监听 %d 秒..." % DURATION)
    c.loop_start()
    time.sleep(DURATION)
    c.loop_stop()
    c.disconnect()
    raw_cvt = [x for x in seen_cvt if "FILE-TERM-01" in x[1] or "MODBUS-TERM-01" in x[1]]
    print("=" * 70)
    print("原始 terminal/+/th 消息数: %d" % len(seen_raw))
    print("转换后(含本组设备) /mqtt-iot 消息数: %d" % len(raw_cvt))
    for _, t, p in seen_raw[:3]:
        print("  sample raw:", t, json.dumps(p, ensure_ascii=False)[:140])
    for _, t, p in raw_cvt[:3]:
        print("  sample cvt:", t, json.dumps(p, ensure_ascii=False)[:140])
    devs = set()
    for _, t, p in seen_raw:
        if isinstance(p, dict) and p.get("device_id"):
            devs.add(p["device_id"])
    print("原始消息中的 device_id 字段:", devs if devs else "无(规则将不命中!)")


if __name__ == "__main__":
    main()
