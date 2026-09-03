# -*- coding: utf-8 -*-
"""决定性实验：验证 EMQX 主题层级匹配语义。
订阅 A(6段,同g5网关) / 订阅 B(5段) 分别测试能否收到 /mqtt-iot/{dev}/properties/report(5段)。
结论将决定根因是"主题层级不匹配"还是"平台消费故障"。"""
import json
import time
import threading
import paho.mqtt.client as mqtt

BROKER = "172.16.4.211"
PORT = 9783
TOPIC = "/mqtt-iot/TEST9ABC/properties/report"

recv = {"sub6": [], "sub5": [], "sub_gw": []}

c6 = mqtt.Client(client_id="diag_sub6", protocol=mqtt.MQTTv311)
c6.username_pw_set("test", "123456")
c5 = mqtt.Client(client_id="diag_sub5", protocol=mqtt.MQTTv311)
c5.username_pw_set("test", "123456")


def on_msg6(client, userdata, msg):
    recv["sub6"].append(msg.topic)


def on_msg5(client, userdata, msg):
    recv["sub5"].append(msg.topic)


c6.on_message = on_msg6
c5.on_message = on_msg5
c6.connect(BROKER, PORT, 30)
c5.connect(BROKER, PORT, 30)
c6.loop_start()
c5.loop_start()
time.sleep(1)

# g5 网关同款订阅: /mqtt-iot/+/+/properties/report  (6段含前导空)
sub6 = "/mqtt-iot/+/+/properties/report"
# 5段对照: /mqtt-iot/+/properties/report
sub5 = "/mqtt-iot/+/properties/report"
r1, _ = c6.subscribe(sub6, qos=1)
r2, _ = c5.subscribe(sub5, qos=1)
print("订阅6段(%s): %s  订阅5段(%s): %s" % (sub6, r1, sub5, r2))
time.sleep(1)

pub = mqtt.Client(client_id="diag_pub", protocol=mqtt.MQTTv311)
pub.username_pw_set("test", "123456")
pub.connect(BROKER, PORT, 30)
pub.loop_start()
time.sleep(0.5)
info = pub.publish(TOPIC, json.dumps({"deviceId": "TEST9ABC", "properties": {"temperature": 1}}), qos=1)
info.wait_for_publish()
print("已发布(5段主题):", TOPIC)

time.sleep(3)
print("\n=== 实验结果 ===")
print("6段订阅(%s) 收到: %d 条 %s" % (sub6, len(recv["sub6"]), recv["sub6"][:3]))
print("5段订阅(%s) 收到: %d 条 %s" % (sub5, len(recv["sub5"]), recv["sub5"][:3]))

pub.disconnect()
c6.loop_stop()
c5.loop_stop()
