# -*- coding: utf-8 -*-
"""JetLinks 接入终端：文件监听温湿度 + JetLinks 官方 MQTT 协议适配 + 平台下行控制

两种上报模式（对应课程两种适配路线）：
  --mode jetlinks : 终端直接按 JetLinks 物模型报文格式上报（路线 2：调整终端报文格式）
                    主题 /{productId}/{deviceId}/properties/report
                    报文 {"properties":{"temperature":25.5,"humidity":60.2}}
  --mode emqx     : 终端保持原始报文格式上报 terminal/{deviceId}/th，
                    由 EMQX 规则引擎转换为 JetLinks 物模型格式（路线 1：EMQX 规则转换）

下行控制（两种模式通用，终端直接订阅 JetLinks 主题）：
  - function/invoke   : 平台功能按钮下发，如 setInterval(设置上报间隔秒数)
  - properties/read   : 平台读取属性，终端回复当前温湿度
  - properties/write  : 平台修改属性，终端写入本地文件并回复
  所有回复主题 = 原主题 + /reply，messageId 与下行一致。

用法：
    python jetlinks_terminal.py                                   # 默认: jetlinks 模式
    python jetlinks_terminal.py --mode emqx                       # EMQX 规则转换模式
    python jetlinks_terminal.py --product mqtt-iot --device FILE-TERM-01 --interval 5
"""
import argparse
import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt

import config
from mqtt_base import MqttReporter  # 仅复用原始格式上报（emqx 模式）
from terminal_file import SensorFileHandler  # 复用文件监听逻辑

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("jetlinks-terminal")

PRODUCT_ID = "mqtt-iot"       # JetLinks 产品 ID（可在平台创建，默认 mqtt-iot）
DEVICE_ID = "FILE-TERM-01"    # JetLinks 设备 ID（需与平台中创建设备的 ID 一致）


class JetLinksReporter:
    """JetLinks 官方协议客户端：JetLinks 主题上报 + 下行控制处理"""

    def __init__(self, product_id: str, device_id: str,
                 host: str, port: int, username: str, password: str,
                 report_interval: int = 5, data_file: str = None):
        self.product_id = product_id
        self.device_id = device_id
        self.host = host
        self.port = port
        self.report_interval = report_interval
        self.data_file = data_file

        # 属性主题
        self.topic_prop_report = f"/{product_id}/{device_id}/properties/report"
        self.topic_prop_read = f"/{product_id}/{device_id}/properties/read"
        self.topic_prop_write = f"/{product_id}/{device_id}/properties/write"
        self.topic_func_invoke = f"/{product_id}/{device_id}/function/invoke"
        self.topic_online = f"/{product_id}/{device_id}/online"
        self.topic_offline = f"/{product_id}/{device_id}/offline"

        # JetLinks 官方 MQTT 协议要求 clientId 必须等于设备 ID
        client_id = device_id
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id, clean_session=True)
        self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        # 遗嘱：设备异常掉线时平台能感知
        self.client.will_set(self.topic_offline,
                             json.dumps({"deviceId": device_id}), qos=1, retain=False)

        # 最新属性（供 read/write 回复）
        self.latest = {"temperature": None, "humidity": None}
        self._connected = False

    # ---------- 连接 ----------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            client.publish(self.topic_online,
                           json.dumps({"deviceId": self.device_id}), qos=1)
            client.subscribe([(self.topic_func_invoke, 1),
                              (self.topic_prop_read, 1),
                              (self.topic_prop_write, 1)])
            log.info("已连接 MQTT %s:%d，已订阅控制主题", self.host, self.port)
            log.info("  功能调用: %s", self.topic_func_invoke)
            log.info("  读取属性: %s / 修改属性: %s", self.topic_prop_read, self.topic_prop_write)
        else:
            log.error("MQTT 连接失败 rc=%s", rc)

    def start(self):
        self.client.connect_async(self.host, self.port, 60)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.publish(self.topic_offline,
                                json.dumps({"deviceId": self.device_id}), qos=1)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------- 上报 ----------
    def report(self, temperature: float, humidity: float) -> bool:
        """兼容文件监听 handler 的调用接口"""
        self.report_jetlinks(temperature, humidity)
        return True

    def report_jetlinks(self, temperature: float, humidity: float):
        """JetLinks 物模型报文（MQTT Broker 接入需带 deviceId）：
        {"deviceId":"FILE-TERM-01","properties":{"temperature":..,"humidity":..}}"""
        payload = {"deviceId": self.device_id,
                   "properties": {"temperature": round(temperature, 2),
                                  "humidity": round(humidity, 2)}}
        self.latest.update(payload["properties"])
        info = self.client.publish(self.topic_prop_report,
                                   json.dumps(payload), qos=1)
        log.info("JetLinks 上报 -> %s  %s", self.topic_prop_report, payload)

    # ---------- 下行控制 ----------
    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.warning("下行消息不是合法 JSON: %s", msg.payload)
            return
        log.info("收到平台下行 -> %s  %s", topic, data)

        if topic == self.topic_func_invoke:
            self._handle_function(data)
        elif topic == self.topic_prop_read:
            self._handle_read(data)
        elif topic == self.topic_prop_write:
            self._handle_write(data)

    def _handle_function(self, data):
        message_id = data.get("messageId", "")
        function_id = data.get("functionId", "")
        inputs = self._normalize_inputs(data.get("inputs", {}))

        if function_id == "setInterval":
            seconds = int(inputs.get("interval", inputs.get("seconds", 5)))
            seconds = max(1, min(3600, seconds))
            self.report_interval = seconds
            output = f"上报间隔已设置为 {seconds} 秒"
            log.info("功能执行: %s -> %s", function_id, output)
            self._reply(f"{self.topic_func_invoke}/reply",
                        message_id, output, success=True)
        else:
            output = f"未知功能: {function_id}"
            log.warning(output)
            self._reply(f"{self.topic_func_invoke}/reply",
                        message_id, output, success=False)

    def _handle_read(self, data):
        message_id = data.get("messageId", "")
        log.info("平台读取属性 -> 回复当前值")
        payload = {"messageId": message_id,
                   "deviceId": self.device_id,
                   "properties": self.latest,
                   "success": True}
        self.client.publish(f"{self.topic_prop_read}/reply",
                            json.dumps(payload), qos=1)
        log.info("回复 -> %s/reply  %s", self.topic_prop_read, payload)

    def _handle_write(self, data):
        message_id = data.get("messageId", "")
        props = {k: float(v) for k, v in data.get("properties", {}).items()
                 if v is not None}
        # 写入本地 JSON 文件，由文件监听（watchdog + 周期轮询）自动触发上报
        merged = {"temperature": self.latest.get("temperature"),
                  "humidity": self.latest.get("humidity")}
        merged.update(props)
        temp = merged["temperature"] if merged["temperature"] is not None else 25.0
        hum = merged["humidity"] if merged["humidity"] is not None else 60.0
        if self.data_file:
            try:
                os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump({"temperature": temp, "humidity": hum}, f,
                              ensure_ascii=False, indent=2)
                log.info("平台修改属性 -> 已写入本地文件: %s", props)
            except OSError as e:
                log.error("写入文件失败: %s", e)
        self._reply(f"{self.topic_prop_write}/reply", message_id,
                    output="", success=True, extra={"properties": props})

    def _reply(self, topic, message_id, output, success=True, extra=None):
        payload = {"messageId": message_id,
                   "deviceId": self.device_id,
                   "success": success}
        if output:
            payload["output"] = output
        if extra:
            payload.update(extra)
        self.client.publish(topic, json.dumps(payload), qos=1)
        log.info("回复 -> %s  %s", topic, payload)

    @staticmethod
    def _normalize_inputs(inputs):
        """JetLinks inputs 可能是 {"interval":5} 或 [{"name":"interval","value":5}]"""
        if isinstance(inputs, dict):
            return inputs
        if isinstance(inputs, list):
            return {i.get("name"): i.get("value") for i in inputs if isinstance(i, dict)}
        return {}


def main():
    parser = argparse.ArgumentParser(description="JetLinks 接入终端（文件监听 + 下行控制）")
    parser.add_argument("--product", default=PRODUCT_ID, help="JetLinks 产品 ID")
    parser.add_argument("--device", default=DEVICE_ID, help="JetLinks 设备 ID")
    parser.add_argument("--mode", choices=["jetlinks", "emqx"], default="jetlinks",
                        help="上报模式：jetlinks=直接物模型格式(默认)；emqx=原始格式由EMQX规则转换")
    parser.add_argument("--interval", type=int, default=5, help="轮询周期秒数(默认5，可被平台 setInterval 动态修改)")
    parser.add_argument("--file", default=os.path.join("data", "sensor_data.json"),
                        help="温湿度数据文件 (默认 data/sensor_data.json)")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 服务器端口")
    parser.add_argument("--user", default=config.MQTT_USER, help="MQTT 用户名")
    parser.add_argument("--passwd", default=config.MQTT_PASS, help="MQTT 密码")
    args = parser.parse_args()

    host = args.host or config.MQTT_HOST
    port = args.port or config.MQTT_PORT
    file_path = os.path.abspath(args.file)

    if not os.path.exists(file_path):
        log.error("数据文件不存在: %s", file_path)
        sys.exit(1)

    reporter = JetLinksReporter(args.product, args.device, host, port,
                                args.user, args.passwd,
                                report_interval=args.interval,
                                data_file=file_path)
    reporter.start()

    # emqx 模式额外维护一个原始格式上报通道（复用 MqttReporter，与昨日终端一致）
    orig_reporter = None
    if args.mode == "emqx":
        orig_reporter = MqttReporter(args.device, "file", host=host, port=port,
                                     username=args.user, password=args.passwd)
        orig_reporter.start()

    # 文件监听：变化即上报（复用 terminal_file 的 handler 逻辑）
    handler = SensorFileHandler(orig_reporter or reporter, file_path, 0.01)
    observer = __import__("watchdog.observers", fromlist=["Observer"]).Observer()
    observer.schedule(handler, os.path.dirname(file_path) or ".", recursive=False)
    observer.start()

    log.info("=" * 60)
    log.info("JetLinks 接入终端启动")
    log.info("  产品ID=%s  设备ID=%s  模式=%s", args.product, args.device, args.mode)
    log.info("  属性上报主题: /%s/%s/properties/report", args.product, args.device)
    log.info("  监听文件: %s  轮询间隔: %s 秒(可被平台setInterval修改)", file_path, reporter.report_interval)
    log.info("=" * 60)

    try:
        # 先上报一次当前值
        handler.read_and_report(force=True)
        next_report = time.time()
        while True:
            time.sleep(0.5)
            # 动态读取间隔：平台 setInterval 修改后立即生效
            if time.time() >= next_report:
                handler.read_and_report()
                next_report = time.time() + max(1, reporter.report_interval)
    except KeyboardInterrupt:
        log.info("收到退出信号")
    finally:
        observer.stop()
        observer.join()
        reporter.stop()
        if orig_reporter:
            orig_reporter.stop()


if __name__ == "__main__":
    main()
