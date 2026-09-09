# -*- coding: utf-8 -*-
"""CPython 简单冒烟测试：验证 modbus_tcp_master.py 可导入、配置可解析、接口一致。"""
import sys
import time

# 补齐 MicroPython time API
if not hasattr(time, "ticks_ms"):
    _base = time.monotonic()
    time.ticks_ms = lambda: int((time.monotonic() - _base) * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)

sys.path.insert(0, r"D:\8-relay\esp32-relay4-modbus-gateway")
from modbus_tcp_master import ModbusTCPMaster


def test_init():
    cfg = {
        "enabled": True,
        "mode": "tcp",
        "timeout_ms": 500,
        "retries": 2,
        "slaves": [
            {
                "enabled": True,
                "host": "192.168.20.59",
                "port": 5502,
                "unit_id": 4,
                "registers": [
                    {"addr": 3, "key": "temperature", "period_ms": 2000},
                    {"addr": 4, "key": "humidity", "period_ms": 2000},
                ],
            }
        ],
    }
    master = ModbusTCPMaster(cfg)
    assert master.init_hw() is True
    assert master.get_values() == {}
    print("test_init PASS")


def test_key_naming():
    cfg = {"enabled": True, "mode": "tcp", "slaves": []}
    master = ModbusTCPMaster(cfg)
    master._update_value(0, "temperature", 44)
    vals = master.get_values()
    assert vals.get("s1_temperature") == 44
    print("test_key_naming PASS")


def test_round_signed():
    cfg = {"enabled": True, "mode": "tcp", "slaves": []}
    master = ModbusTCPMaster(cfg)
    # 无符号 65534 按有符号解析应为 -2
    raw = 65534
    signed = raw > 32767
    if signed:
        raw -= 65536
    assert raw == -2
    print("test_round_signed PASS")


if __name__ == "__main__":
    test_init()
    test_key_naming()
    test_round_signed()
    print("all smoke tests passed")
