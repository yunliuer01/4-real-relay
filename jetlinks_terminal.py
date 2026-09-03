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
  共性要点（平台联调必做）：回复 invoke/reply 之后，终端还须主动补发一条
  /properties/report 更新控制后的最新状态，平台运行状态才能即时刷新验证。

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
import threading
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
        # 平台控制后的主动上报通道（默认直接走 JetLinks 物模型主题；
        # emqx 模式下由 main() 替换为原始格式上报，走 EMQX 规则转换链路）
        self.upstream_report = None

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
            self._apply_interval(seconds)
            output = f"上报间隔已设置为 {seconds} 秒"
            log.info("功能执行: %s -> %s", function_id, output)
            self._reply(f"{self.topic_func_invoke}/reply",
                        message_id, output, success=True)
        elif function_id in ("setTH", "setTempHum", "setSensorValue"):
            # 平台一键设置温湿度：写目标 + 回复，由文件监听(watchdog + 周期轮询)
            # 或下一次轮询自动触发补发上报，与手动改文件同一链路
            self._handle_set_th(message_id, inputs)
        else:
            output = f"未知功能: {function_id}"
            log.warning(output)
            self._reply(f"{self.topic_func_invoke}/reply",
                        message_id, output, success=False)

    # ---------- 设备动作钩子（子类可重写以适配不同数据源：文件 / Modbus / OPC-UA ...） ----------
    def _read_current_values(self):
        """读取当前温度/湿度作为未填字段的兜底值。返回 (t, h)，失败返回 (None, None)。

        默认实现从 data_file 读 JSON；MODBUS 子类重写为读寄存器。"""
        if not self.data_file:
            return (None, None)
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return float(d.get("temperature")), float(d.get("humidity"))
        except Exception:
            return (None, None)

    def _apply_values(self, temperature: float, humidity: float) -> bool:
        """把温度/湿度写入真实数据源。返回是否成功。

        默认实现写 data_file；MODBUS 子类重写为写保持寄存器。"""
        if not self.data_file:
            return False
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump({"temperature": temperature, "humidity": humidity}, f,
                          ensure_ascii=False, indent=2)
            log.info("平台设置温湿度 -> 已写入文件: %.2f℃ / %.2f%%RH",
                     temperature, humidity)
            return True
        except OSError as e:
            log.error("写入文件失败: %s", e)
            return False

    def _apply_interval(self, seconds: int):
        """应用平台下发的 setInterval。默认仅更新 self.report_interval。

        MODBUS 子类重写时同步修改 ModbusCollector.interval。"""
        self.report_interval = seconds

    def _handle_set_th(self, message_id, inputs):
        """平台功能按钮设置温湿度。

        做三件事：写文件 + 回复 invoke/reply + 延迟补发 properties/report。
        - 写文件保证与手动改文件同一数据源（watchdog/轮询兜底上报）；
        - invoke/reply 让平台显示功能执行结果；
        - 主动 properties/report 让平台运行状态即时刷新（不依赖文件监听时序）。
        参数可只传一个，另一个沿用文件里的当前值。
        """
        inputs = self._normalize_inputs(inputs)  # 防御：统一 dict 形式
        try:
            temp = inputs.get("temperature")
            hum = inputs.get("humidity")
            if temp is None and hum is None:
                raise ValueError("至少填写 temperature 或 humidity 之一")
            if temp is not None:
                temp = float(temp)
                if not (-40.0 <= temp <= 80.0):
                    raise ValueError(f"温度 {temp} 超出范围 [-40, 80]℃")
            if hum is not None:
                hum = float(hum)
                if not (0.0 <= hum <= 100.0):
                    raise ValueError(f"湿度 {hum} 超出范围 [0, 100]%RH")
        except (TypeError, ValueError) as e:
            log.warning("功能执行失败: %s", e)
            self._reply(f"{self.topic_func_invoke}/reply", message_id,
                        output=str(e), success=False)
            return
        # 未填写的参数沿用当前真实值（FILE 读文件，MODBUS 读寄存器）
        cur_t, cur_h = self._read_current_values()
        new_temp = round(temp if temp is not None
                         else (cur_t if cur_t is not None else 25.0), 2)
        new_hum = round(hum if hum is not None
                        else (cur_h if cur_h is not None else 60.0), 2)
        file_ok = self._apply_values(new_temp, new_hum)
        # 先回复 invoke/reply（平台据此显示功能执行结果），
        # 再主动补发一条 properties/report 更新设备最新状态（老师提示的共性问题）。
        # 文件监听(watchdog+轮询)或下一次 MODBUS 轮询仍作为兜底，双保险。
        self._reply(f"{self.topic_func_invoke}/reply", message_id,
                    output=f"OK: temperature={new_temp}, humidity={new_hum}"
                           + ("" if file_ok else " (写入失败)"),
                    success=file_ok)
        if file_ok:
            self._schedule_upstream_report(new_temp, new_hum)

    def _schedule_upstream_report(self, temperature: float, humidity: float,
                                  delay: float = 0.5):
        """延迟补发一条属性上报。

        为什么用 threading.Timer 而不是直接发：
        - 不能在 on_message 回调里 time.sleep()（会阻塞 paho 网络线程）；
        - 延迟 0.5s 让 invoke/reply 先落地，平台先看到功能执行成功，
          随后立即收到 properties/report，运行状态即时刷新。
        上报通道：
        - jetlinks 模式 -> 直接发 /{productId}/{deviceId}/properties/report
        - emqx 模式    -> 发原始格式 terminal/{deviceId}/th，由 EMQX 规则转换
        """
        target = self.upstream_report or self.report_jetlinks

        def _do_report():
            try:
                target(temperature, humidity)
                log.info("控制后主动上报完成: %.2f℃ / %.2f%%RH", temperature, humidity)
            except Exception as e:
                log.error("控制后主动上报失败: %s", e)

        timer = threading.Timer(delay, _do_report)
        timer.daemon = True
        timer.start()

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
        # 由子类实现的"应用属性"接口（FILE 写文件，MODBUS 写寄存器）；
        # 未填写的字段从当前真实值兜底
        cur_t, cur_h = self._read_current_values()
        merged = {"temperature": cur_t, "humidity": cur_h}
        merged.update(props)
        temp = merged["temperature"] if merged["temperature"] is not None else 25.0
        hum = merged["humidity"] if merged["humidity"] is not None else 60.0
        file_ok = self._apply_values(temp, hum)
        if file_ok:
            log.info("平台修改属性 -> 已应用: %s", props)
        self._reply(f"{self.topic_prop_write}/reply", message_id,
                    output="", success=file_ok, extra={"properties": props})
        # 与 function/invoke 同理：回复后补发最新属性，平台运行状态即时刷新
        if file_ok:
            self._schedule_upstream_report(temp, hum)

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
        """JetLinks inputs 可能是 {"interval":5}、[{"name":"interval","value":5}]，
        或全部参数包一层 data/params：{"data":{...}} / {"params":{...}} /
        [{"name":"data","value":{...}}] / [{"name":"params","value":{...}}]
        （实测：UI 走 data 包裹，REST /function/{id} 走 params 包裹）"""
        if isinstance(inputs, dict):
            result = inputs
        elif isinstance(inputs, list):
            result = {i.get("name"): i.get("value") for i in inputs if isinstance(i, dict)}
        else:
            result = {}
        # 兼容包一层 data 或 params 的调用形式
        if isinstance(result.get("data"), dict):
            result = result["data"]
        if isinstance(result.get("params"), dict):
            result = result["params"]
        return result


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
        # 控制后的主动补发上报也走原始格式（terminal/{deviceId}/th），
        # 由 EMQX 规则引擎统一转换为 JetLinks 物模型格式，链路保持一致
        reporter.upstream_report = orig_reporter.report

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
            # 周期轮询必须强制上报（force=True），保证平台运行状态页面"时时刻刻"都有数据，
            # 与文件变化触发的"按阈值上报"是两条独立路径，互不干扰：
            #   - 周期轮询：兜底刷新，让平台持续看到最新值
            #   - watchdog回调：手动改文件时立即触发（编辑器保存抖动由300ms防抖处理）
            if time.time() >= next_report:
                handler.read_and_report(force=True)
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
