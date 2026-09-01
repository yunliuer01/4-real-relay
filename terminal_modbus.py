# -*- coding: utf-8 -*-
"""Modbus 采集终端：通过 Modbus TCP 轮询读取温湿度寄存器，检测到变化后上报 MQTT

默认连接实验平台从站 192.168.20.59:5502，本组寄存器 0x0005。
寄存器编码（16 位）：高 8 位 = 温度℃，低 8 位 = 湿度%RH
    例：0x1F3D = 2597 -> 温度 31℃ / 湿度 61%RH

用法：
    python terminal_modbus.py                            # 连接实验平台 192.168.20.59:5502，寄存器 0x0005
    python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020   # 连接本地模拟服务测试
    python terminal_modbus.py --reg 0x0006               # 换用本组其他寄存器
"""
import argparse
import logging
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

import config
from mqtt_base import MqttReporter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("modbus-terminal")

# 实验平台 Modbus 从站默认参数
DEFAULT_MODBUS_HOST = "192.168.20.59"
DEFAULT_MODBUS_PORT = 5502
DEFAULT_REG = 0x0005          # 本组寄存器地址（0x0000~0x0009，各组独立，避免冲突）


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
                 reg: int, deadband: float, interval: float, slave_id: int):
        self.reporter = reporter
        self.modbus_host = modbus_host
        self.modbus_port = modbus_port
        self.reg = reg
        self.deadband = deadband
        self.interval = interval
        self.slave_id = slave_id
        self.client = ModbusTcpClient(host=modbus_host, port=modbus_port,
                                      timeout=5, retries=2)
        self.last_values = (None, None)

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

    def poll_once(self) -> bool:
        """采集一次，变化则上报。返回是否成功采集"""
        raw = self._read_register(self.reg)
        if raw is None:
            return False

        temperature, humidity = decode_register(raw)

        last_t, last_h = self.last_values
        if (last_t is None
                or abs(temperature - last_t) >= self.deadband
                or abs(humidity - last_h) >= self.deadband):
            if self.reporter.report(temperature, humidity):
                self.last_values = (temperature, humidity)
        else:
            log.info("数值未变化（%.0f℃ / %.0f%%RH，寄存器=0x%04X），不上报",
                     temperature, humidity, raw)
        return True

    def run(self):
        log.info("开始轮询 Modbus TCP %s:%d，本组寄存器 0x%04X（高8位温度/低8位湿度），周期=%.1fs",
                 self.modbus_host, self.modbus_port, self.reg, self.interval)
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


def main():
    parser = argparse.ArgumentParser(description="Modbus 采集终端：读取 Modbus TCP 温湿度并上报 MQTT")
    parser.add_argument("--device-id", default="MODBUS-TERM-01", help="终端设备 ID")
    parser.add_argument("--modbus-host", default=DEFAULT_MODBUS_HOST,
                        help=f"Modbus TCP 从站地址 (默认 {DEFAULT_MODBUS_HOST})")
    parser.add_argument("--modbus-port", type=int, default=DEFAULT_MODBUS_PORT,
                        help=f"Modbus TCP 从站端口 (默认 {DEFAULT_MODBUS_PORT})")
    parser.add_argument("--reg", type=parse_int, default=DEFAULT_REG,
                        help="本组保持寄存器地址，支持 0x 十六进制 (默认 0x0005)")
    parser.add_argument("--slave-id", type=int, default=1, help="Modbus 从站 ID (默认 1)")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询周期秒 (默认 2)")
    parser.add_argument("--deadband", type=float, default=1.0,
                        help="变化阈值，超过才上报 (默认 1，即 1℃/1%%RH)")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 服务器端口")
    args = parser.parse_args()

    if not (0x0000 <= args.reg <= 0x0009):
        log.warning("寄存器地址 0x%04X 超出实验平台范围 0x0000~0x0009，注意与其他小组冲突", args.reg)

    reporter = MqttReporter(args.device_id, "modbus",
                            host=args.host, port=args.port)
    reporter.start()
    log.info("Modbus 采集终端已启动  设备ID=%s  寄存器=0x%04X", args.device_id, args.reg)
    log.info("MQTT 服务器 %s:%d  数据主题 %s",
             args.host or config.MQTT_HOST, args.port or config.MQTT_PORT,
             config.data_topic(args.device_id))

    collector = ModbusCollector(
        reporter, args.modbus_host, args.modbus_port,
        args.reg, args.deadband, args.interval, args.slave_id,
    )
    try:
        collector.run()
    except KeyboardInterrupt:
        log.info("收到退出信号")
    finally:
        collector.close()
        reporter.stop()


if __name__ == "__main__":
    main()
