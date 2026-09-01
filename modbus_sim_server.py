# -*- coding: utf-8 -*-
"""Modbus TCP 模拟服务：模拟实验平台，用于本地测试采集终端

模拟实验平台从站（实际平台为 192.168.20.59:5502）：
- 保持寄存器 0x0000 ~ 0x0009 共 10 个，每个小组对应一个，互不冲突
- 本组使用 0x0005（可通过 GROUP_REG 修改）
- 寄存器编码（16 位）：高 8 位 = 温度℃，低 8 位 = 湿度%RH
- 每隔 UPDATE_INTERVAL 秒随机小幅变化，模拟现场传感器

配合采集终端使用：
    python modbus_sim_server.py                                              # 窗口1：启动模拟服务(端口5020)
    python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020     # 窗口2：采集终端
"""
import logging
import random

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("modbus-sim")

HOST = "0.0.0.0"
PORT = 5020          # 502 是特权端口，Windows 下用 5020 方便测试
REG_COUNT = 10       # 实验平台寄存器 0x0000 ~ 0x0009
GROUP_REG = 0x0005   # 本组寄存器
UPDATE_INTERVAL = 5  # 数值更新周期（秒）

# 初始值：25℃ / 60%RH -> 高8位=25(0x19) 低8位=60(0x3C) -> 0x193C = 6460
INIT_TEMP = 25
INIT_HUM = 60


def set_register(context, address, value, slave_id=1):
    """写入保持寄存器（兼容 pymodbus 3.7 驼峰与新版本下划线 API）"""
    slave = context[slave_id]
    setter = getattr(slave, "setValues", None) or getattr(slave, "set_values")
    setter(3, address, [value])


def update_values(context, slave_id=1):
    """周期性小幅随机变化本组寄存器，供采集终端检测变化"""
    temp, hum = INIT_TEMP, INIT_HUM
    import time
    while True:
        time.sleep(UPDATE_INTERVAL)
        try:
            # 温度 ±1℃ 内波动，湿度 ±3%RH 内波动
            temp = max(0, min(99, temp + random.randint(-1, 1)))
            hum = max(0, min(99, hum + random.randint(-3, 3)))
            value = ((temp & 0xFF) << 8) | (hum & 0xFF)
            set_register(context, GROUP_REG, value)
            log.info("模拟传感器更新：寄存器 0x%04X = 0x%04X -> 温度=%d℃ 湿度=%d%%RH",
                     GROUP_REG, value, temp, hum)
        except Exception as e:
            log.exception("更新寄存器失败: %s", e)


def main():
    # 全部 10 个寄存器初始为打包值，其他小组寄存器保持静态（模拟平台其他组）
    initial = [((INIT_TEMP & 0xFF) << 8) | (INIT_HUM & 0xFF)] * REG_COUNT
    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, initial),
        zero_mode=True,  # 寄存器地址从 0 直接映射，与实验平台 0x0000~0x0009 一致
    )
    context = ModbusServerContext(slaves=store, single=True)

    import threading
    t = threading.Thread(target=update_values, args=(context,), daemon=True)
    t.start()

    log.info("Modbus TCP 模拟服务启动 %s:%d (从站ID=1，寄存器 0x0000~0x0009，本组 0x%04X，高8位温度/低8位湿度)",
             HOST, PORT, GROUP_REG)
    StartTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    main()
