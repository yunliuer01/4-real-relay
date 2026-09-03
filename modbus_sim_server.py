# -*- coding: utf-8 -*-
"""Modbus TCP 模拟服务：模拟实验平台，用于本地测试采集终端

模拟实验平台从站（实际平台为 192.168.20.59:5502）：
- 多从站模式：从站 ID = 组号（本组默认 5，可 --groups 调整数量），
  模拟"各组 IP/端口相同、用组号作从站 ID 区分，避免采集/设置冲突"的真实约定
- 每个从站内保持寄存器 0x0000 ~ 0x0009 共 10 个，各组寄存器地址互不冲突
- 本组寄存器 0x0005（可通过 GROUP_REG 修改）
- 寄存器编码（16 位）：高 8 位 = 温度℃，低 8 位 = 湿度%RH
- 每隔 UPDATE_INTERVAL 秒随机小幅变化，模拟现场传感器
- 外部写入保护：检测到外部（采集终端 setTH / modbus_write 等）写入寄存器后，
  暂停随机漂移 --hold 秒（默认 60s），保证平台设置的值在运行状态页可见；
  漂移恢复时从当前寄存器值继续，不会跳变

配合采集终端使用（本组从站 ID=5）：
    python modbus_sim_server.py                                              # 窗口1：启动模拟服务(端口5020)
    python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020     # 窗口2：采集终端(默认 slave-id=5)
    python modbus_write.py --temp 26 --hum 58 --host 127.0.0.1 --port 5020   # 窗口3：写入本组从站5寄存器
"""
import argparse
import logging
import random
import threading
import time

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
UPDATE_INTERVAL = 5  # 数值更新周期（秒）
SIM_GROUPS = 9       # 模拟从站数（组号 1~9），与实验平台小组数一致

# 初始值：25℃ / 60%RH -> 高8位=25(0x19) 低8位=60(0x3C) -> 0x193C = 6460
INIT_TEMP = 25
INIT_HUM = 60

HOLD_SECONDS = 60  # 外部写入后随机漂移暂停时长（秒），可用 --hold 调整

# 外部写入保护：数据块被网络侧写入时记录"保持截止时间"，漂移线程到点前跳过
_hold_lock = threading.Lock()
_hold_until = 0.0
_internal = threading.local()  # 漂移线程写寄存器时置标志，避免自己触发 hold


class HoldDataBlock(ModbusSequentialDataBlock):
    """外部写入触发 hold：hold 期间随机漂移暂停，保证 setTH 设置值可见"""

    def setValues(self, address, values, **kwargs):
        if not getattr(_internal, "flag", False):
            global _hold_until
            with _hold_lock:
                _hold_until = time.time() + HOLD_SECONDS
            log.info("检测到外部写入寄存器 -> 随机漂移暂停 %d 秒", HOLD_SECONDS)
        super().setValues(address, values, **kwargs)


def build_slave_context() -> ModbusSlaveContext:
    """创建一个从站上下文：全部 10 个寄存器初始为打包值"""
    initial = [((INIT_TEMP & 0xFF) << 8) | (INIT_HUM & 0xFF)] * REG_COUNT
    return ModbusSlaveContext(
        hr=HoldDataBlock(0, initial),
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


def update_values(context, slave_id: int):
    """某个从站周期性小幅随机变化本组寄存器，供采集终端检测变化

    外部写入后的 hold 期间跳过漂移；恢复漂移时先重读寄存器当前值，
    从该值继续小幅变化（可能已被 setTH 修改），避免跳变。
    """
    while True:
        time.sleep(UPDATE_INTERVAL)
        try:
            with _hold_lock:
                if time.time() < _hold_until:
                    continue  # hold 期间不漂移，保持外部设置的值
            # 重读当前寄存器值，同步为漂移起点
            raw = get_register(context, slave_id, GROUP_REG)
            temp = (raw >> 8) & 0xFF
            hum = raw & 0xFF
            temp = max(0, min(99, temp + random.randint(-1, 1)))
            hum = max(0, min(99, hum + random.randint(-3, 3)))
            value = ((temp & 0xFF) << 8) | (hum & 0xFF)
            _internal.flag = True  # 内部写入，不触发 hold
            try:
                set_register(context, slave_id, GROUP_REG, value)
            finally:
                _internal.flag = False
            log.info("从站ID=%d 模拟更新：寄存器 0x%04X = 0x%04X -> 温度=%d℃ 湿度=%d%%RH",
                     slave_id, GROUP_REG, value, temp, hum)
        except Exception as e:
            log.exception("更新寄存器失败: %s", e)


def main():
    global HOLD_SECONDS
    parser = argparse.ArgumentParser(description="Modbus TCP 多从站模拟服务（从站 ID = 组号）")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口 (默认 5020)")
    parser.add_argument("--groups", type=int, default=SIM_GROUPS,
                        help=f"模拟从站组数 1..N (默认 {SIM_GROUPS})")
    parser.add_argument("--hold", type=int, default=HOLD_SECONDS,
                        help=f"外部写入后随机漂移暂停秒数 (默认 {HOLD_SECONDS})")
    args = parser.parse_args()

    HOLD_SECONDS = max(0, args.hold)

    slaves = {g: build_slave_context() for g in range(1, args.groups + 1)}
    context = ModbusServerContext(slaves=slaves, single=False)

    # 每个从站独立线程做随机波动
    for g in range(1, args.groups + 1):
        t = threading.Thread(target=update_values, args=(context, g), daemon=True)
        t.start()

    log.info("Modbus TCP 模拟服务启动 %s:%d (多从站 ID=1~%d，本组=%d，寄存器 0x0000~0x0009，本组 0x%04X，高8位温度/低8位湿度)",
             HOST, args.port, args.groups, GROUP_SLAVE_ID, GROUP_REG)
    log.info("本组采集终端请用 --slave-id %d；写入工具同样默认从站 ID=%d",
             GROUP_SLAVE_ID, GROUP_SLAVE_ID)
    StartTcpServer(context=context, address=(HOST, args.port))


if __name__ == "__main__":
    main()
