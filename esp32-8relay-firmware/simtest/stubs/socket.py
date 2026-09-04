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
        return self._s.connect(address)

    def setsockopt(self, level, opt, value):
        return self._s.setsockopt(level, opt, value)
