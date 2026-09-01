# -*- coding: utf-8 -*-
"""MQTT 监视工具：订阅终端上报主题，实时打印收到的数据（调试用）

用法：
    python mqtt_monitor.py                     # 订阅 terminal/#
    python mqtt_monitor.py -t "terminal/#" -v
"""
import argparse
import logging

import paho.mqtt.client as mqtt

import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor")


def main():
    parser = argparse.ArgumentParser(description="订阅并打印终端上报的 MQTT 消息")
    parser.add_argument("-t", "--topic", default="terminal/#", help="订阅主题 (默认 terminal/#)")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 服务器端口")
    args = parser.parse_args()

    host = args.host or config.MQTT_HOST
    port = args.port or config.MQTT_PORT

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("已连接 %s:%d，订阅主题: %s", host, port, args.topic)
            client.subscribe(args.topic, qos=1)
        else:
            log.error("连接失败: %s", reason_code)

    def on_message(client, userdata, msg):
        print(f"[收到] {msg.topic}: {msg.payload.decode('utf-8', 'replace')}", flush=True)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="monitor-viewer")
    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, config.MQTT_KEEPALIVE)
    client.loop_forever()


if __name__ == "__main__":
    main()
