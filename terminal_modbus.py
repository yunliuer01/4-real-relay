# -*- coding: utf-8 -*-
"""Modbus 采集终端：通过 Modbus TCP 轮询读取温湿度寄存器，检测到变化后上报 MQTT

默认连接实验平台从站 192.168.20.59:5502，本组寄存器 0x0005。
寄存器编码（16 位）：高 8 位 = 温度℃，低 8 位 = 湿度%RH
    例：0x1F3D = 2597 -> 温度 31℃ / 湿度 61%RH

对接 JetLinks（产品 mqtt-iot，与文件终端同产品下的第二个设备 MODBUS-TERM-01）：
    数据仍按原始格式上报 terminal/MODBUS-TERM-01/th，
    由 EMQX 规则 rule_lfx_modbus 转换为 JetLinks 物模型报文转发到
    /mqtt-iot/MODBUS-TERM-01/properties/report（见 jetlinks_rule.sql）。
    终端连接 MQTT 后额外发布 /mqtt-iot/MODBUS-TERM-01/online，平台显示在线。

多小组共享 Modbus 从站约定（重要）：
    IP/端口各组相同（192.168.20.59:5502），每组用自己的【组号】作为从站 ID（slave/unit id），
    不再统一用 1，避免各组采集与设置互相冲突。本组组号 = 5。

用法：
    python terminal_modbus.py                            # 连接实验平台 192.168.20.59:5502，从站ID=5，寄存器 0x0005
    python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020   # 连接本地模拟服务测试
    python terminal_modbus.py --reg 0x0006               # 换用本组其他寄存器
    python terminal_modbus.py --slave-id 1               # 临时用其他从站 ID（默认已是组号 5）
"""
import argparse
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

import config
from jetlinks_terminal import JetLinksReporter
from mqtt_base import MqttReporter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("modbus-terminal")

# 实验平台 Modbus 从站默认参数
DEFAULT_MODBUS_HOST = "192.168.20.59"
DEFAULT_MODBUS_PORT = 5502
DEFAULT_REG = 0x0005          # 本组寄存器地址（0x0000~0x0009，各组独立，避免冲突）
GROUP_NO = 5                  # 本小组组号：作为 Modbus 从站 ID（各组 IP/端口相同，用组号区分从站）
DEFAULT_SLAVE_ID = GROUP_NO


def parse_int(text: str) -> int:
    """支持 0x 十六进制或十进制寄存器地址，如 0x0005 或 5"""
    return int(text, 0)


def decode_register(raw: int):
    """解析 16 位寄存器：高 8 位温度，低 8 位湿度"""
    raw &= 0xFFFF
    return (raw >> 8) & 0xFF, raw & 0xFF


class ModbusCollector:
    """Modbus TCP 轮询采集 + 变化检测上报"""

    def __init__(self, reporter: MqttReporter, modbus_host: str, modbus_port: int,
                 reg: int, deadband: float, interval: float, slave_id: int,
                 heartbeat: float = 30.0):
        self.reporter = reporter
        self.modbus_host = modbus_host
        self.modbus_port = modbus_port
        self.reg = reg
        self.deadband = deadband
        self.interval = interval
        self.slave_id = slave_id
        self.heartbeat = heartbeat  # 心跳周期：值长期不变时也强制上报，保证平台运行状态持续有数据
        self.client = ModbusTcpClient(host=modbus_host, port=modbus_port,
                                      timeout=5, retries=2)
        self.last_values = (None, None)
        self._last_report = 0.0

    # ---------- Modbus 读取 ----------
    def _read_register(self, address: int):
        """读单个保持寄存器，返回原始数值；失败返回 None"""
        try:
            rr = self.client.read_holding_registers(address=address,
                                                    count=1,
                                                    slave=self.slave_id)
        except ModbusException as e:
            log.error("Modbus 读取异常 (地址 0x%04X): %s", address, e)
            return None
        if rr.isError():
            log.error("Modbus 返回错误 (地址 0x%04X): %s", address, rr)
            return None
        return rr.registers[0]

    def poll_once(self, force: bool = False) -> bool:
        """采集一次，变化(或心跳强制)则上报。返回是否成功采集"""
        raw = self._read_register(self.reg)
        if raw is None:
            return False

        temperature, humidity = decode_register(raw)

        last_t, last_h = self.last_values
        changed = (last_t is None
                   or abs(temperature - last_t) >= self.deadband
                   or abs(humidity - last_h) >= self.deadband)
        now = time.time()
        if changed or force or (now - self._last_report >= self.heartbeat):
            if self.reporter.report(temperature, humidity):
                self.last_values = (temperature, humidity)
                self._last_report = now
        else:
            log.info("数值未变化（%.0f℃ / %.0f%%RH，寄存器=0x%04X），不上报",
                     temperature, humidity, raw)
        return True

    def run(self):
        log.info("开始轮询 Modbus TCP %s:%d，本组寄存器 0x%04X（高8位温度/低8位湿度），周期=%.1fs，心跳=%.0fs",
                 self.modbus_host, self.modbus_port, self.reg, self.interval, self.heartbeat)
        while True:
            if not self.client.connected:
                log.info("连接 Modbus 服务 %s:%d ...", self.modbus_host, self.modbus_port)
                if not self.client.connect():
                    log.warning("Modbus 连接失败，%ds 后重试", int(self.interval))
                    time.sleep(self.interval)
                    continue

            self.poll_once()
            time.sleep(self.interval)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


class ModbusJetLinksReporter(JetLinksReporter):
    """MODBUS 终端的 JetLinks 协议下行适配。

    复用 JetLinksReporter 的精确 clientId 连接、订阅、回复、补发等基础设施，
    通过覆写三个钩子把 setTH / properties/write 的"应用目标"从 JSON 文件改到 Modbus 保持寄存器。
    寄存器编码协议（与本终端读侧一致）：高 8 位温度℃、低 8 位湿度%RH。
    """

    def __init__(self, *args, modbus_client: ModbusTcpClient, reg: int,
                 collector: "ModbusCollector", **kwargs):
        # data_file=None 让父类不再尝试文件 I/O
        super().__init__(*args, data_file=None, **kwargs)
        self.modbus_client = modbus_client
        self.reg = reg
        self.collector = collector  # 用于 setInterval 同步轮询周期、读寄存器时取 slave_id

    # ---- 三个钩子：把"数据源"从 JSON 文件改为 Modbus 保持寄存器 ----
    def _read_current_values(self):
        """读 Modbus 寄存器当前值作为未填字段的兜底值。"""
        try:
            rr = self.modbus_client.read_holding_registers(
                address=self.reg, count=1, slave=self.collector.slave_id)
            if rr.isError():
                log.warning("[modbus-jl] 读寄存器 0x%04X 失败: %s", self.reg, rr)
                return (None, None)
            raw = rr.registers[0] & 0xFFFF
            return float((raw >> 8) & 0xFF), float(raw & 0xFF)
        except Exception as e:
            log.warning("[modbus-jl] 读寄存器异常: %s", e)
            return (None, None)

    def _apply_values(self, temperature: float, humidity: float) -> bool:
        """把温度湿度写到 Modbus 保持寄存器 0x%04X，高 8 位温度、低 8 位湿度。"""
        try:
            t = int(round(temperature)) & 0xFF
            h = int(round(humidity)) & 0xFF
            raw = (t << 8) | h
            rr = self.modbus_client.write_register(
                address=self.reg, value=raw, slave=self.collector.slave_id)
            if rr.isError():
                log.error("[modbus-jl] 写寄存器 0x%04X 失败: %s", self.reg, rr)
                return False
            log.info("[modbus-jl] setTH 已写入寄存器 0x%04X: %.2f℃ / %.2f%%RH (raw=0x%04X)",
                     self.reg, temperature, humidity, raw)
            return True
        except Exception as e:
            log.error("[modbus-jl] 写寄存器异常: %s", e)
            return False

    def _apply_interval(self, seconds: int):
        """setInterval 同时更新 self.report_interval 和 ModbusCollector 的轮询周期。"""
        self.report_interval = seconds
        if self.collector is not None:
            self.collector.interval = seconds
            log.info("[modbus-jl] 同步更新 ModbusCollector.interval = %ds", seconds)


def main():
    parser = argparse.ArgumentParser(description="Modbus 采集终端：读取 Modbus TCP 温湿度并上报 MQTT/JetLinks")
    parser.add_argument("--device-id", default="MODBUS-TERM-01", help="终端设备 ID (JetLinks mqtt-iot 产品下)")
    parser.add_argument("--product", default="mqtt-iot", help="JetLinks 产品 ID")
    parser.add_argument("--modbus-host", default=DEFAULT_MODBUS_HOST,
                        help=f"Modbus TCP 从站地址 (默认 {DEFAULT_MODBUS_HOST})")
    parser.add_argument("--modbus-port", type=int, default=DEFAULT_MODBUS_PORT,
                        help=f"Modbus TCP 从站端口 (默认 {DEFAULT_MODBUS_PORT})")
    parser.add_argument("--reg", type=parse_int, default=DEFAULT_REG,
                        help="本组保持寄存器地址，支持 0x 十六进制 (默认 0x0005)")
    parser.add_argument("--slave-id", type=int, default=DEFAULT_SLAVE_ID,
                        help=f"Modbus 从站 ID = 组号，避免各组冲突 (默认 {DEFAULT_SLAVE_ID})")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询周期秒 (默认 2)")
    parser.add_argument("--heartbeat", type=float, default=30.0,
                        help="心跳秒数：值长期不变也强制上报，保证平台持续有数据 (默认 30)")
    parser.add_argument("--deadband", type=float, default=1.0,
                        help="变化阈值，超过才上报 (默认 1，即 1℃/1%%RH)")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 服务器端口")
    parser.add_argument("--user", default=config.MQTT_USER, help="MQTT 用户名")
    parser.add_argument("--passwd", default=config.MQTT_PASS, help="MQTT 密码")
    args = parser.parse_args()

    if not (0x0000 <= args.reg <= 0x0009):
        log.warning("寄存器地址 0x%04X 超出实验平台范围 0x0000~0x0009，注意与其他小组冲突", args.reg)

    reporter = MqttReporter(args.device_id, "modbus",
                            host=args.host, port=args.port,
                            username=args.user, password=args.passwd,
                            jetlinks_online={"product_id": args.product})
    reporter.start()
    log.info("Modbus 采集终端已启动  设备ID=%s  寄存器=0x%04X  从站ID=%d",
             args.device_id, args.reg, args.slave_id)
    log.info("MQTT 服务器 %s:%d  数据主题 %s  (EMQX规则转换 -> /%s/%s/properties/report)",
             args.host or config.MQTT_HOST, args.port or config.MQTT_PORT,
             config.data_topic(args.device_id), args.product, args.device_id)

    collector = ModbusCollector(
        reporter, args.modbus_host, args.modbus_port,
        args.reg, args.deadband, args.interval, args.slave_id,
        heartbeat=args.heartbeat,
    )

    # JetLinks 官方协议下行适配：精确 clientId 订阅 /mqtt-iot/MODBUS-TERM-01/function/invoke
    # 等控制主题，setTH 写本组寄存器 0x%04X，setInterval 同步到 collector.interval。
    modbus_jl = ModbusJetLinksReporter(
        product_id=args.product, device_id=args.device_id,
        host=args.host or config.MQTT_HOST, port=args.port or config.MQTT_PORT,
        username=args.user, password=args.passwd,
        modbus_client=collector.client, reg=args.reg, collector=collector,
    )
    modbus_jl.start()
    log.info("JetLinks 下行处理已启动 (精确clientId=%s，订阅 function/invoke 等)",
             args.device_id)

    try:
        collector.run()
    except KeyboardInterrupt:
        log.info("收到退出信号")
    finally:
        modbus_jl.stop()
        collector.close()
        reporter.stop()


if __name__ == "__main__":
    main()
