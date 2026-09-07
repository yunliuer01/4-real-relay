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


# ============================================================
# Modbus RTU 仿真总线（供固件 UART 桩使用 + run_sim 控制注入）
# ============================================================
# 从站注册中心：slave_id -> {"regs": {addr:int}, "down":bool, "crc_bad":bool, "exc":int|None}
_SLAVES: dict = {}


def _crc16(data):
    """Modbus RTU CRC16（与固件 modbus_master.py 相同算法）"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _crc_bytes(data):
    crc = _crc16(data)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _exc_frame(sid, func, exc_code):
    frame = bytes([sid, func | 0x80, exc_code])
    return frame + _crc_bytes(frame)


def _handle_slave_request(frame: bytes, rxbuf: bytearray):
    """主站发来一帧请求 -> 模拟从站应答（写入 rxbuf）。

    与真实 RTU 总线一致的行为：
    - 从站不存在 / down / 请求 CRC 错 -> 无应答（主站超时）
    - addr 不在寄存器表 -> 回异常码 0x02
    - 注册了 exc -> 回指定异常码
    - crc_bad -> 应答故意写错 CRC
    """
    if len(frame) < 8:
        return
    sid, func = frame[0], frame[1]
    slave = _SLAVES.get(sid)
    if slave is None or slave.get("down"):
        return
    req_crc = frame[-2] | (frame[-1] << 8)
    if _crc16(frame[:-2]) != req_crc:
        return  # 坏请求帧不回
    if func not in (3, 4):
        return
    addr = (frame[2] << 8) | frame[3]
    count = (frame[4] << 8) | frame[5]
    if count != 1:
        rxbuf += _exc_frame(sid, func, 0x02)
        return
    exc = slave.get("exc")
    if exc:
        rxbuf += _exc_frame(sid, func, exc)
        return
    if addr not in slave["regs"]:
        rxbuf += _exc_frame(sid, func, 0x02)
        return
    val = slave["regs"][addr] & 0xFFFF
    resp = bytes([sid, func, 0x02, (val >> 8) & 0xFF, val & 0xFF])
    resp += _crc_bytes(resp)
    if slave.get("crc_bad"):
        # 把最后一个 CRC 字节取反，制造 CRC 校验失败
        resp = resp[:-1] + bytes([resp[-1] ^ 0xFF])
    rxbuf += resp


class UART:
    """MicroPython UART 桩：环回内存总线，后端接仿真 Modbus 从站。

    固件侧使用的 API：UART(id, baudrate=, tx=, rx=, bits=, parity=, stop=, timeout=)
    + any() / read() / read(nbytes) / write(bytes) / deinit()
    """

    def __init__(self, id, baudrate=9600, tx=None, rx=None,
                 bits=8, parity=None, stop=1, timeout=0, **kwargs):
        self.id = int(id)
        self.baudrate = int(baudrate)
        self.tx = tx
        self.rx = rx
        self.timeout = timeout
        self._rxbuf = bytearray()

    def any(self):
        return len(self._rxbuf) > 0

    def read(self, nbytes=None):
        if not self._rxbuf:
            return b""
        if nbytes is None or nbytes <= 0:
            out = bytes(self._rxbuf)
            self._rxbuf.clear()
            return out
        out = bytes(self._rxbuf[:nbytes])
        del self._rxbuf[:nbytes]
        return out

    def readinto(self, buf, nbytes=None):
        data = self.read(nbytes)
        buf[:len(data)] = data
        return len(data)

    def write(self, data):
        frame = bytes(data)
        _handle_slave_request(frame, self._rxbuf)
        return len(frame)

    def deinit(self):
        self._rxbuf.clear()


# ---------- run_sim 控制 API（仅测试进程使用，固件不会调用） ----------
def sim_modbus_reset():
    _SLAVES.clear()


def sim_modbus_add_slave(slave_id, regs):
    """注册一个仿真从站：{addr: value}。已存在则覆盖。"""
    _SLAVES[int(slave_id)] = {"regs": {int(k): int(v) for k, v in regs.items()},
                              "down": False, "crc_bad": False, "exc": None}


def sim_modbus_set(slave_id, addr, value):
    """改写某从站寄存器值（模拟传感器读数变化）。"""
    s = _SLAVES.get(int(slave_id))
    if s is None:
        raise ValueError("no such sim slave %s" % slave_id)
    s["regs"][int(addr)] = int(value)


def sim_modbus_get(slave_id, addr):
    s = _SLAVES.get(int(slave_id))
    if s is None or int(addr) not in s["regs"]:
        return None
    return s["regs"][int(addr)]


def sim_modbus_down(slave_id, v=None):
    """置 True 模拟从站掉线/无应答；None 表示查询。"""
    s = _SLAVES.get(int(slave_id))
    if s is None:
        return None
    if v is not None:
        s["down"] = bool(v)
    return s["down"]


def sim_modbus_crc_bad(slave_id, v=None):
    s = _SLAVES.get(int(slave_id))
    if s is None:
        return None
    if v is not None:
        s["crc_bad"] = bool(v)
    return s["crc_bad"]


def sim_modbus_exc(slave_id, code=None):
    """注入 Modbus 异常响应；code=None 清除。"""
    s = _SLAVES.get(int(slave_id))
    if s is None:
        return None
    if code is not None:
        s["exc"] = int(code)
    else:
        s.pop("exc", None)
    return s.get("exc")
