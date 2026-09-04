# -*- coding: utf-8 -*-
"""time 桩：提供 MicroPython 常用时间 API（ticks_* / sleep_ms 等）。

与 CPython time 的差异点补齐，运行 run_sim 时会注册为 sys.modules['time']。
注意：本模块 import 真实 time 必须发生在注册覆盖之前。
"""
from __future__ import annotations

import time as _t

# 单调时钟基准（用于 ticks_ms 的“时长”语义）
_MONO_BASE = _t.monotonic()
_EPOCH = _t.time()


def time():
    return _t.time()


def monotonic():
    return _t.monotonic()


def localtime(secs=None):
    return _t.localtime(secs)


def sleep(s):
    _t.sleep(s)


def sleep_ms(ms):
    _t.sleep(ms / 1000.0)


def sleep_us(us):
    _t.sleep(us / 1_000_000.0)


def ticks_ms():
    # 相对单调毫秒，避免 CPython 无 ticks_ms 的问题
    return int((_t.monotonic() - _MONO_BASE) * 1000)


def ticks_add(ticks, delta):
    return ticks + delta


def ticks_diff(ticks1, ticks2):
    return ticks1 - ticks2


def gmtime(secs=None):
    return _t.gmtime(secs)


def mktime(t):
    return _t.mktime(t)
