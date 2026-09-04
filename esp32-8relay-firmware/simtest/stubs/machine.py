# -*- coding: utf-8 -*-
"""machine 桩：Pin / reset / 以及模拟继电器与按键状态。

仿真约定：
- Pin.value(0)=导通/按下，value(1)=断开/松开（与固件低电平吸合语义一致）。
- 通过 `machine.reset()` 抛 SimReset，由 run_sim 捕获后重新执行固件（模拟掉电重启）。
"""
from __future__ import annotations

# 保存所有 Pin 的当前电平：pin_id -> 0/1
PINS: dict = {}

# 记录哪些 pin 是输入（按键），由 run_sim 注入按下状态
SW1_PIN = 8  # 由固件配置读取，桩默认记录


class SimReset(BaseException):
    """模拟 machine.reset()：从固件内部抛出，run_sim 捕获后重新启动固件。"""


class Pin:
    IN = 0
    OUT = 1
    OPEN_DRAIN = 2
    PULL_UP = 1
    PULL_DOWN = 0
    PULL_HOLD = 3
    LOW = 0
    HIGH = 1

    def __init__(self, id, mode=-1, pull=-1, value=None, drive=None):
        self.id = int(id)
        self.mode = mode
        self.pull = pull
        PINS.setdefault(self.id, 1)
        if value is not None:
            PINS[self.id] = int(value) & 1

    def value(self, v=None):
        if v is None:
            return PINS.get(self.id, 1)
        PINS[self.id] = int(v) & 1
        return None

    def on(self):
        return self.value(0)  # 低电平吸合 -> on

    def off(self):
        return self.value(1)

    def low(self):
        return self.value(0)

    def high(self):
        return self.value(1)

    def irq(self, *a, **k):
        pass

    # ---------- 仿真辅助 ----------
    @classmethod
    def sim_write(cls, pin_id, v):
        """run_sim 直接写某个引脚（如模拟按键按下/松开）。"""
        PINS[int(pin_id)] = int(v) & 1

    @classmethod
    def sim_read(cls, pin_id):
        return PINS.get(int(pin_id), 1)


def sim_write(pin_id, v):
    """模块级便捷函数：驱动某个引脚电平（run_sim 使用）。"""
    Pin.sim_write(pin_id, v)


def sim_read(pin_id):
    """模块级便捷函数：读取当前某个引脚电平（run_sim 使用）。"""
    return Pin.sim_read(pin_id)


def reset():
    """模拟硬件复位：抛出 SimReset，由 run_sim 捕获并重启固件主程序。"""
    raise SimReset("machine.reset() simulated")


def freq(*a, **k):
    return 160_000_000


def unique_id():
    return bytes([0x24, 0x0A, 0xC4, 0x00, 0x11, 0x22, 0x33, 0x44])


def soft_reset():
    reset()
