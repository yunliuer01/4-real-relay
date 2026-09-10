# -*- coding: utf-8 -*-
"""socket 桩：完整 re-export 真实 socket，仅替换 socket 类为 bytes-only + 端口重映射。

- send/sendall/write 只接受 bytes（与 MicroPython 一致，传 str 抛 TypeError）。
- bind 时端口按 PORT_MAP 重映射（默认 80 -> 18080），避免占用真实特权端口。
- 其余属性（getaddrinfo/create_connection/error 等）原样透传，保证 paho/urllib 可用。
"""
from __future__ import annotations

import socket as _real

# ---- 全量透传真实 socket 的属性/函数/常量 ----
for _n in dir(_real):
    if _n.startswith("__"):
        continue
    globals()[_n] = getattr(_real, _n)

# run_sim 会设置：把固件绑定的端口映射到测试端口
PORT_MAP = {80: 18080}

# 仿真的「虚拟可达端点」集合：{(host, port)}。
# 进程内 MQTT 总线没有真实 TCP 监听，但语义上它是「可达」的，
# inject_stubs 会把占位 broker 地址注册进来（见 run_sim_modbus.inject_stubs）。
VIRTUAL_ENDPOINTS = set()


def sim_add_virtual_endpoint(host, port):
    """声明 (host, port) 是仿真内虚拟可达端点（connect 直接成功）。"""
    VIRTUAL_ENDPOINTS.add((str(host), int(port)))


def sim_clear_virtual_endpoints():
    VIRTUAL_ENDPOINTS.clear()


def is_virtual_endpoint(address):
    """address 可能是 (host, port) 或 getaddrinfo 的 4/5 元组。"""
    try:
        host, port = address[0], int(address[1])
    except Exception:
        return False
    return (str(host), port) in VIRTUAL_ENDPOINTS


class socket:
    """薄封装：send 仅接受 bytes，bind 支持端口重映射。"""

    def __init__(self, family=_real.AF_INET, type=_real.SOCK_STREAM, proto=0, fileno=None):
        if fileno is not None:
            self._s = _real.socket(fileno=fileno)
        else:
            self._s = _real.socket(family, type, proto)

    def __getattr__(self, name):
        return getattr(self._s, name)

    def bind(self, address):
        try:
            host, port = address
            new_port = PORT_MAP.get(int(port), int(port))
        except Exception:
            return self._s.bind(address)
        return self._s.bind((host, new_port))

    def accept(self):
        c, addr = self._s.accept()
        w = socket.__new__(socket)
        w._s = c
        return w, addr

    def send(self, data):
        if isinstance(data, str):
            raise TypeError("MicroPython socket.send requires bytes, got str")
        return self._s.send(data)

    def sendall(self, data):
        if isinstance(data, str):
            raise TypeError("MicroPython socket.sendall requires bytes, got str")
        return self._s.sendall(data)

    def write(self, data):
        if isinstance(data, str):
            raise TypeError("MicroPython socket.write requires bytes, got str")
        return self._s.sendall(data)

    def read(self, n=-1):
        return self._s.recv(n if n >= 0 else 4096)

    def recv(self, n):
        return self._s.recv(n)

    def close(self):
        return self._s.close()

    def settimeout(self, t):
        return self._s.settimeout(t)

    def setblocking(self, flag):
        return self._s.setblocking(flag)

    def connect(self, address):
        # 仿真专用：进程内虚拟端点（见 VIRTUAL_ENDPOINTS）直接返回成功。
        # 固件 v6.0.5 起在建连前会做一次带超时的 TCP 可达性预检，而 loopback
        # 模式下 MQTT 走的是进程内总线、根本没有真实 TCP 监听（占位地址
        # 127.0.0.1:1），预检必然失败会把固件挡在门外。虚拟端点让「可达性」
        # 与仿真语义一致：总线在，端点就算可达。
        if is_virtual_endpoint(address):
            return None
        return self._s.connect(address)

    def setsockopt(self, level, opt, value):
        return self._s.setsockopt(level, opt, value)
