# -*- coding: utf-8 -*-
"""Modbus TCP 模拟服务：模拟实验平台，用于本地测试采集终端

模拟实验平台从站（实际平台为 192.168.20.59:5502）：
- 多从站模式：从站 ID = 组号（本组默认 5，可 --groups 调整数量），
  模拟"各组 IP/端口相同、用组号作从站 ID 区分，避免采集/设置冲突"的真实约定
- 每个从站内保持寄存器 0x0000 ~ 0x0009 共 10 个，各组寄存器地址互不冲突
- 本组寄存器 0x0005（可通过 GROUP_REG 修改）
- 寄存器编码（16 位）：高 8 位 = 温度℃，低 8 位 = 湿度%RH
- 数值保持稳定：启动后为 25℃/60%RH，只由外部写入（采集终端 setTH /
  modbus_write 等）改变，不再自动随机漂移——保证 JetLinks 运行状态页
  数值稳定、完全受平台命令控制

配合采集终端使用（本组从站 ID=5）：
    python modbus_sim_server.py                                              # 窗口1：启动模拟服务(端口5020)
    python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020     # 窗口2：采集终端(默认 slave-id=5)
    python modbus_write.py --temp 26 --hum 58 --host 127.0.0.1 --port 5020   # 窗口3：写入本组从站5寄存器
"""
import argparse
import logging

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("modbus-sim")

HOST = "0.0.0.0"
PORT = 5020          # 502 是特权端口，Windows 下用 5020 方便测试
REG_COUNT = 10       # 每个从站寄存器 0x0000 ~ 0x0009
GROUP_REG = 0x0005   # 本组寄存器
GROUP_SLAVE_ID = 5   # 本组从站 ID = 组号
SIM_GROUPS = 9       # 模拟从站数（组号 1~9），与实验平台小组数一致

# 初始值：25℃ / 60%RH -> 高8位=25(0x19) 低8位=60(0x3C) -> 0x193C = 6460
# 说明：寄存器值保持初始值或最近一次外部写入值，不随时间自动变化
INIT_TEMP = 25
INIT_HUM = 60


def build_slave_context() -> ModbusSlaveContext:
    """创建一个从站上下文：全部 10 个寄存器初始为打包值"""
    initial = [((INIT_TEMP & 0xFF) << 8) | (INIT_HUM & 0xFF)] * REG_COUNT
    return ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, initial),
        zero_mode=True,  # 寄存器地址从 0 直接映射，与实验平台 0x0000~0x0009 一致
    )


def set_register(ctx, slave_id: int, address: int, value: int):
    """写入某个从站的保持寄存器"""
    store = ctx[slave_id]
    setter = getattr(store, "setValues", None) or getattr(store, "set_values")
    setter(3, address, [value])


def get_register(ctx, slave_id: int, address: int) -> int:
    """读取某个从站保持寄存器当前值"""
    store = ctx[slave_id]
    getter = getattr(store, "getValues", None) or getattr(store, "get_values")
    return getter(3, address, 1)[0]


def main():
    parser = argparse.ArgumentParser(description="Modbus TCP 多从站模拟服务（从站 ID = 组号）")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口 (默认 5020)")
    parser.add_argument("--groups", type=int, default=SIM_GROUPS,
                        help=f"模拟从站组数 1..N (默认 {SIM_GROUPS})")
    args = parser.parse_args()

    slaves = {g: build_slave_context() for g in range(1, args.groups + 1)}
    context = ModbusServerContext(slaves=slaves, single=False)

    log.info("Modbus TCP 模拟服务启动 %s:%d (多从站 ID=1~%d，本组=%d，寄存器 0x0000~0x0009，本组 0x%04X，高8位温度/低8位湿度)",
             HOST, args.port, args.groups, GROUP_SLAVE_ID, GROUP_REG)
    log.info("寄存器值保持稳定(初始 %d℃/%d%%RH)，仅由外部写入改变：modbus_write.py / 平台 setTH 下发",
             INIT_TEMP, INIT_HUM)
    log.info("本组采集终端请用 --slave-id %d；写入工具同样默认从站 ID=%d",
             GROUP_SLAVE_ID, GROUP_SLAVE_ID)
    StartTcpServer(context=context, address=(HOST, args.port))


if __name__ == "__main__":
    main()
