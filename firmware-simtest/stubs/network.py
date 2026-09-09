# -*- coding: utf-8 -*-
"""network 桩：WLAN STA/AP 模拟。

run_sim 可通过本模块控制：
- 设置 AP/STA MAC（config("mac")）
- STA 连接目标 SSID 与是否成功（sim_set_sta_ok）
- 模拟断网/恢复（sim_set_sta_connected）
"""
from __future__ import annotations

STA_IF = 0
AP_IF = 1
AUTH_OPEN = 0

# 仿真注入的 STA 状态
_sta_target_ssid = None
_sta_connected = False      # 是否已经连上路由器
_sta_ok = True              # 是否允许连接（False 模拟密码错误/不在范围）
_mac_sta = bytes([0x24, 0x0a, 0xc4, 0x00, 0x12, 0x34])
_mac_ap = bytes([0x24, 0x0a, 0xc4, 0x00, 0xab, 0xcd])
_ap_ifconfig = ("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.1")
_sta_ifconfig = ("192.168.31.100", "255.255.255.0", "192.168.31.1", "192.168.31.1")

# 最近一次创建的 STA WLAN 实例（run_sim 用于判断固件是否进入正常模式）
_last_sta = None


def sim_set_sta_connected(v: bool):
    global _sta_connected
    _sta_connected = bool(v)


def sim_sta_connected():
    """固件 STA 是否已 active 且连上（True 表示处于正常模式，非配网模式）。"""
    return _last_sta is not None and bool(_last_sta._active) and bool(_sta_connected)


def sim_set_sta_ok(v: bool):
    """False 模拟连不上 WiFi（超时后进配网 / 断线后一直重试）。"""
    global _sta_ok
    _sta_ok = bool(v)


def sim_set_sta_mac(mac: bytes):
    global _mac_sta
    _mac_sta = bytes(mac)


def sim_set_sta_ip(ip: str):
    global _sta_ifconfig
    _sta_ifconfig = (ip, "255.255.255.0", ip[: ip.rfind(".") + 1] + "1", ip)


class WLAN:
    def __init__(self, iface):
        global _last_sta
        self._iface = iface
        self._active = False
        self._cfg = {}
        if iface == STA_IF:
            _last_sta = self

    @property
    def iface(self):
        return self._iface

    def active(self, v=None):
        if v is None:
            return self._active
        self._active = bool(v)
        return None

    def isconnected(self):
        if not self._active:
            return False
        if self._iface == STA_IF:
            return _sta_connected
        # AP 永远是“连接状态”语义（有客户端与否不关心）
        return True

    def connect(self, ssid, pwd=None):
        global _sta_connected, _sta_target_ssid
        _sta_target_ssid = ssid
        if self._iface != STA_IF:
            return
        if _sta_ok:
            _sta_connected = True
        else:
            _sta_connected = False

    def disconnect(self):
        global _sta_connected
        if self._iface == STA_IF:
            _sta_connected = False

    def config(self, param=None, **kwargs):
        if isinstance(param, str):
            if param == "mac":
                return _mac_sta if self._iface == STA_IF else _mac_ap
            if param == "essid":
                return self._cfg.get("essid", "")
            if param == "channel":
                return self._cfg.get("channel", 6)
            raise ValueError("unsupported config param: %s" % param)
        # 关键字形式：设置 AP 参数
        self._cfg.update(kwargs)
        return None

    def ifconfig(self, addr=None):
        if addr is not None:
            if self._iface == STA_IF:
                global _sta_ifconfig
                _sta_ifconfig = tuple(str(x) for x in addr)
            else:
                global _ap_ifconfig
                _ap_ifconfig = tuple(str(x) for x in addr)
            return None
        return _sta_ifconfig if self._iface == STA_IF else _ap_ifconfig

    def status(self, *a):
        # 固件 connect_wifi 用「status()==1000(驱动空闲)才发起 connect」做门控，
        # 纯 bool 会永远不等于 1000 导致连接永不发起（曾致仿真 WiFi 超时）。
        # 约定：未连接=1000(IDLE，允许发起 connect)，已连接=1005(非门控/非失败码)。
        if self._iface == STA_IF:
            return 1000 if not _sta_connected else 1005
        return True

    def scan(self):
        return []


def phy_mode(*a):
    return None
