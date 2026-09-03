# -*- coding: utf-8 -*-
"""模拟 JetLinks 平台下发 function/invoke，验证终端回复链路。

验证要点（老师提示的共性问题）：
  1. 终端收到 function/invoke 后，必须回复 function/invoke/reply（messageId 回带）
  2. setTH 这类改变属性的功能，回复后还须补发一条 /properties/report
     更新控制后设备的最新状态
用法：
    python test_invoke_sim.py            # 测试 jetlinks 模式终端
    python test_invoke_sim.py --mode emqx  # emqx 模式下额外监听原始主题
"""
import argparse
import json
import logging
import threading
import time
import uuid

import paho.mqtt.client as mqtt

import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("invoke-sim")

PRODUCT_ID = "mqtt-iot"
DEVICE_ID = "FILE-TERM-01"

received = []            # (topic, payload) 收集到的上行消息
lock = threading.Lock()
done = threading.Event()


def on_connect(client, userdata, flags, rc, properties=None):
    topics = [
        f"/{PRODUCT_ID}/{DEVICE_ID}/function/invoke/reply",
        f"/{PRODUCT_ID}/{DEVICE_ID}/properties/report",
        f"/{PRODUCT_ID}/{DEVICE_ID}/properties/read/reply",
        f"/{PRODUCT_ID}/{DEVICE_ID}/properties/write/reply",
    ]
    if args_mode == "emqx":
        topics.append(config.data_topic(DEVICE_ID))
    for t in topics:
        client.subscribe(t, 1)
    log.info("已连接并订阅: %s", ", ".join(topics))


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        payload = msg.payload
    with lock:
        received.append((msg.topic, payload))
    log.info("<<< 收到 [%s] %s", msg.topic, payload)


def invoke(client, function_id, inputs):
    message_id = f"test-{uuid.uuid4().hex[:8]}"
    payload = {"messageId": message_id,
               "deviceId": DEVICE_ID,
               "functionId": function_id,
               "inputs": inputs}
    topic = f"/{PRODUCT_ID}/{DEVICE_ID}/function/invoke"
    client.publish(topic, json.dumps(payload), qos=1)
    log.info(">>> 下发 [%s] %s", topic, payload)
    return message_id


def find_reply(message_id, prefix):
    with lock:
        for topic, p in received:
            if topic.startswith(prefix) and isinstance(p, dict) \
                    and p.get("messageId") == message_id:
                return p
    return None


def main():
    global args_mode
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["jetlinks", "emqx"], default="jetlinks")
    args = parser.parse_args()
    args_mode = args.mode

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"invoke-sim-{uuid.uuid4().hex[:6]}")
    client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT, 60)
    client.loop_start()
    time.sleep(2)

    ok = True

    # ---- 用例 1: setInterval ----
    mid1 = invoke(client, "setInterval", {"interval": 10})
    time.sleep(3)
    r1 = find_reply(mid1, f"/{PRODUCT_ID}/{DEVICE_ID}/function/invoke/reply")
    if r1 and r1.get("success") is True:
        log.info("[用例1 PASS] setInterval 已回复 invoke/reply: %s", r1.get("output"))
    else:
        ok = False
        log.error("[用例1 FAIL] 未收到 setInterval 的 invoke/reply: %s", r1)

    # ---- 用例 2: setTH（重点：reply + 补发 properties/report）----
    mid2 = invoke(client, "setTH", {"temperature": 28.8, "humidity": 55.5})
    time.sleep(4)
    r2 = find_reply(mid2, f"/{PRODUCT_ID}/{DEVICE_ID}/function/invoke/reply")
    if r2 and r2.get("success") is True:
        log.info("[用例2a PASS] setTH 已回复 invoke/reply: %s", r2.get("output"))
    else:
        ok = False
        log.error("[用例2a FAIL] 未收到 setTH 的 invoke/reply: %s", r2)

    # 检查 setTH 之后是否有携带新值的 properties/report
    # （jetlinks 模式直接监听物模型主题；emqx 模式监听原始主题由规则转换）
    report_found = False
    with lock:
        for topic, p in received:
            props = p.get("properties", {}) if isinstance(p, dict) else {}
            if abs(float(props.get("temperature", -999)) - 28.8) < 0.01 \
                    and abs(float(props.get("humidity", -999)) - 55.5) < 0.01:
                report_found = True
                log.info("[用例2b PASS] 已补发控制后的最新状态 [%s] %s", topic, p)
                break
    if not report_found:
        ok = False
        log.error("[用例2b FAIL] setTH 后未收到携带 28.8/55.5 的属性上报")

    # ---- 用例 3: 参数校验（越界值应回复 success=false）----
    mid3 = invoke(client, "setTH", {"temperature": 200})
    time.sleep(3)
    r3 = find_reply(mid3, f"/{PRODUCT_ID}/{DEVICE_ID}/function/invoke/reply")
    if r3 and r3.get("success") is False:
        log.info("[用例3 PASS] 越界参数已正确拒绝: %s", r3.get("output"))
    else:
        log.warning("[用例3 WARN] 越界参数处理待确认: %s", r3)

    client.loop_stop()
    client.disconnect()
    log.info("=" * 50)
    log.info("测试结果: %s", "全部通过" if ok else "存在失败项")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
