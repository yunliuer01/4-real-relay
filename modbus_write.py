# -*- coding: utf-8 -*-
"""Modbus 写入工具：把温湿度打包写入本组寄存器（实验演示/测试用）

寄存器编码：高 8 位 = 温度℃，低 8 位 = 湿度%RH

用法：
    python modbus_write.py --temp 26 --hum 58                 # 写入实验平台 0x0005
    python modbus_write.py --temp 26 --hum 58 --host 127.0.0.1 --port 5020   # 写入本地模拟服务
"""
import argparse
import logging

from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("modbus-write")

DEFAULT_HOST = "192.168.20.59"
DEFAULT_PORT = 5502
DEFAULT_REG = 0x0005


def parse_int(text: str) -> int:
    return int(text, 0)


def main():
    parser = argparse.ArgumentParser(description="把温湿度写入本组 Modbus 保持寄存器")
    parser.add_argument("--temp", type=float, required=True, help="温度 ℃ (0~255)")
    parser.add_argument("--hum", type=float, required=True, help="湿度 %RH (0~255)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Modbus 从站地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Modbus 从站端口")
    parser.add_argument("--reg", type=parse_int, default=DEFAULT_REG, help="寄存器地址 (默认 0x0005)")
    parser.add_argument("--slave-id", type=int, default=1, help="从站 ID")
    args = parser.parse_args()

    temp = int(round(args.temp))
    hum = int(round(args.hum))
    if not (0 <= temp <= 255 and 0 <= hum <= 255):
        raise SystemExit("温度/湿度需在 0~255 范围内")

    value = ((temp & 0xFF) << 8) | (hum & 0xFF)
    client = ModbusTcpClient(host=args.host, port=args.port, timeout=5)
    if not client.connect():
        raise SystemExit(f"连接 Modbus 从站失败: {args.host}:{args.port}")
    try:
        rq = client.write_register(address=args.reg, value=value, slave=args.slave_id)
        if rq.isError():
            raise SystemExit(f"写入失败: {rq}")
        log.info("已写入 %s:%d 寄存器 0x%04X = 0x%04X（温度=%d℃ 湿度=%d%%RH）",
                 args.host, args.port, args.reg, value, temp, hum)
    finally:
        client.close()


if __name__ == "__main__":
    main()
