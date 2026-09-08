# -*- coding: utf-8 -*-
"""下行命令测试脚本：PC → 真实 EMQX → 板子 → 继电器动作

用途：
- 验证 broker ACL 是否允许 publish 到 /relay4_lfx/<deviceId>/service/cmd
- 验证板子是否真的订阅 + 处理 + 回复
- 验证继电器 GPIO 是否翻转（看板子串口 + broker /function/post 上的 reply）

运行：
- 默认场景：先发 set_channel(ch=1, state=1)，3s 后 set_channel(ch=1, state=0)
- 命令行可改 channel / state / method / device_id
"""
import argparse
import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt


BROKER_HOST = "172.16.4.211"
BROKER_PORT = 9783
BROKER_USER = "test"
BROKER_PASSWORD = "123456"

PRODUCT_ID = "relay4_lfx"
DEFAULT_DEVICE_ID = os.environ.get("BOARD_DEVICE_ID", "")


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


def make_service_cmd_payload(method, args, message_id=None):
    """构造 direct 模式 service/cmd 的 payload

    两种格式都兼容：
    - JetLinks 风格: {"functionId": method, "messageId": ..., "inputs": [{"name":k,"value":v}]}
    - 简化风格: {"functionId": method, "messageId": ..., "params": {k:v}}
    """
    if message_id is None:
        message_id = "test-%d" % int(time.time() * 1000)
    return {
        "functionId": method,
        "messageId": message_id,
        "inputs": [{"name": k, "value": v} for k, v in args.items()],
        "params": args,
    }


def main():
    parser = argparse.ArgumentParser(description="下行命令测试")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="板上 device_id（不填需从 EMQX 发现）")
    parser.add_argument("--method", default="set_channel", choices=["set_channel", "switch_all"])
    parser.add_argument("--channel", type=int, default=1, help="set_channel 时使用")
    parser.add_argument("--state", type=int, default=1, help="0=关 1=开")
    parser.add_argument("--wait-seconds", type=float, default=6.0, help="开→关 的间隔秒数；<=0 表示只发一条")
    parser.add_argument("--auto-discover", action="store_true",
                        help="未指定 device-id 时通过订阅 $SYS/# /online 发现一次板子")
    args = parser.parse_args()

    # ---- 简单 broker + device-id 发现 ----
    received_msgs = []
    lock = threading.Lock()

    def on_connect(client, userdata, flags, rc, props=None):
        log("connect", "rc=%d" % rc)
        # 订阅本设备下行回执：/function/post（direct 模式 set_reply）
        if args.device_id:
            reply_topic = "%s/%s/function/post" % (PRODUCT_ID, args.device_id)
            client.subscribe(reply_topic, qos=1)
            log("subscribe", reply_topic)

    def on_message(client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:
            payload = repr(msg.payload)
        with lock:
            received_msgs.append((msg.topic, payload, time.time()))
        log("recv", "topic=%s body=%s" % (msg.topic, payload[:300]))

    c = mqtt.Client(client_id="wb-downlink-tester-%d" % int(time.time()),
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(BROKER_USER, BROKER_PASSWORD)
    c.on_connect = on_connect
    c.on_message = on_message

    device_id = args.device_id

    # 如果没指定且允许 discover：订阅 product/<id>/online 一会儿找一台设备
    discovered = []
    if not device_id and args.auto_discover:
        def on_msg_disc(cli, ud, msg):
            if not msg.topic.endswith("/online"):
                return
            parts = msg.topic.split("/")
            if len(parts) >= 2:
                did = parts[1]
                if did not in discovered:
                    discovered.append(did)
                    log("discover", "found device_id=%s" % did)
        c.on_message = on_msg_disc
        log("connect", "connect+discover mode")
        c.connect(BROKER_HOST, BROKER_PORT, 60)
        c.loop_start()
        # 订阅所有 product 在线主题
        c.subscribe("%s/+/online" % PRODUCT_ID, qos=0)
        log("wait", "waiting up to 5s for board to publish online")
        for _ in range(50):
            time.sleep(0.1)
            if discovered:
                break
        if discovered:
            device_id = discovered[0]
        c.loop_stop()
        c.disconnect()
        if not device_id:
            log("fail", "no device discovered on broker")
            return 1

    if not device_id:
        log("fail", "必须先 --device-id <MAC>（板子连上 broker 后会 publish <device_id>/online，可从 EMQX dashboard 看）")
        return 1

    # 改回 on_message
    c.on_message = on_message

    # ---- 测试主流程 ----
    log("test", "target device_id=%s method=%s channel=%s state=%s wait=%ss"
        % (device_id, args.method, args.channel, args.state, args.wait_seconds))

    log("connect", "connecting to broker")
    c.connect(BROKER_HOST, BROKER_PORT, 60)
    c.loop_start()

    cmd_topic = "%s/%s/service/cmd" % (PRODUCT_ID, device_id)

    def send(args_dict):
        payload = make_service_cmd_payload(args.method, args_dict)
        body = json.dumps(payload)
        log("publish", "topic=%s body=%s" % (cmd_topic, body))
        info = c.publish(cmd_topic, body, qos=1)
        info.wait_for_publish(timeout=3)

    if args.method == "set_channel":
        send({"channel": args.channel, "state": args.state})
        if args.wait_seconds > 0:
            time.sleep(args.wait_seconds)
            send({"channel": args.channel, "state": 0 if args.state else 1})
    else:  # switch_all
        send({"state": args.state})
        if args.wait_seconds > 0:
            time.sleep(args.wait_seconds)
            send({"state": 0 if args.state else 1})

    # 多等一会，看 function/post 回执
    time.sleep(2.0)

    c.loop_stop()
    c.disconnect()

    log("done", "received %d message(s) during test" % len(received_msgs))
    for tp, body, ts in received_msgs:
        log("done", "topic=%s" % tp)
        log("done", "  body=%s" % body[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
