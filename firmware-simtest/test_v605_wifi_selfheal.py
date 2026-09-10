# -*- coding: utf-8 -*-
"""test_v605_wifi_selfheal.py —— v6.0.5 自愈 D（WiFi 数据面假死 -> 重新关联）验收

被测固件：esp32-relay4-modbus-gateway/main.py
后端：无需 broker / 无需硬件 —— 只 exec 固件定义，然后直接驱动判决函数。
这样跑得快且完全确定（不依赖 Office-WiFi 的时好时坏）。

背景（2026-09-10 实板实测，独立于 HTTP 冻结与 PUBACK 死锁）：
  板子 isconnected()=True、status()=1010、ifconfig 有合法 IP，但数据面已死：
    - MQTT 建连 SYN 重传耗尽 -> [Errno 113] ECONNABORTED（lwIP ERR_ABRT）
    - 同一时刻 PC 侧 ping 板子大量丢包 / ARP 不应答
    - 手动 w.disconnect()+connect() 重新关联后立刻恢复
  致命点：主循环的「WiFi 断线重连」只在 isconnected() 为 False 时触发，假死下
  它恒为 True -> 板子永远卡在 MQTT 重试里，只能人工复位。

修复（v6.0.5 自愈 D）：连续 MQTT 建连失败 >= MQTT_FAIL_REASSOC 且 WiFi 仍自称
已连接 -> 主动重新关联；带冷却时间避免与真实 broker 故障互殴。

断言：
  S0  前置：固件源码已包含自愈 D 的常量/函数/主循环挂钩/api/info 字段
  S1  未达阈值不动作（mqtt_fail_streak=2 -> 不重新关联）
  S2  达阈值 + WiFi 自称已连接 -> 触发重新关联（disconnect+connect 各一次）
  S3  触发后状态正确（wifi_reassocs+1 / mqtt_fail_streak 清零 / mqtt_retry 归零）
  S4  冷却期内不重复触发
  S5  WiFi 真断线（isconnected=False）时不动手（交给原有重连分支）
  S6  mqtt_connect 失败使 mqtt_fail_streak 递增
  S7  mqtt_connect 成功使 mqtt_fail_streak 清零
  S8  冷启动回归：wifi_reassoc_ms 初值 None 时首次自愈立即触发（不被冷却门误挡）
  S9  自愈 E：网络彻底不可用 -> 硬复位阶梯（时长 / 重新关联次数 / 冷启动豁免）

用法：
    python firmware-simtest/test_v605_wifi_selfheal.py
"""
from __future__ import annotations

import json
import os
import sys
import time as real_time

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, PROJ_ROOT)

import run_sim_modbus as R  # noqa: E402  复用桩注入

FW = os.path.join(R.FW_DIR, "main.py")
WEB_PORT = 18096
# main.py 末尾的启动块（exec 定义时要去掉，否则会真的跑 main()）
BOOT_MARK = "\ntry:\n    main()\nexcept Exception as e:"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  |  " + detail) if detail else ""), flush=True)


def load_fw_defs():
    """exec 固件源码（去掉末尾启动块）拿到全部定义，不触发 main()。"""
    with open(FW, "r", encoding="utf-8") as f:
        src = f.read()
    src = src.replace("\r\n", "\n")
    if BOOT_MARK not in src:
        raise RuntimeError("找不到 main.py 末尾启动块，无法安全 exec")
    src = src.split(BOOT_MARK)[0]
    ns = {"__name__": "_probe"}
    exec(compile(src, FW, "exec"), ns)
    return src, ns


def main():
    print("=" * 78)
    print("v6.0.5 自愈 D 验收：WiFi 假死（isconnected 为真但数据面已死）")
    print("=" * 78)

    # 桩必须先注入：exec 固件时它会 import network/machine/socket/umqtt
    stubs = R.inject_stubs(web_port=WEB_PORT)
    net = stubs["network"]

    # ---------------- S0 静态前置 ----------------
    src, ns = load_fw_defs()
    need = {
        "常量 MQTT_FAIL_REASSOC": "MQTT_FAIL_REASSOC =" in src,
        "常量 WIFI_REASSOC_COOLDOWN_MS": "WIFI_REASSOC_COOLDOWN_MS =" in src,
        "常量 NET_RESET_AFTER_MS": "NET_RESET_AFTER_MS =" in src,
        "函数 wifi_reassoc_if_dead": "def wifi_reassoc_if_dead(" in src,
        "函数 net_dead_track": "def net_dead_track(" in src,
        "函数 net_reset_if_hopeless": "def net_reset_if_hopeless(" in src,
        "主循环挂钩": "wifi_reassoc_if_dead(cfg, now)" in src,
        "主循环自愈E挂钩": "net_reset_if_hopeless(cfg, now)" in src,
        "mqtt_connect 累计失败": "mqtt_fail_streak += 1" in src,
        "api/info 暴露 wifi_reassocs": '"wifi_reassocs": wifi_reassocs' in src,
        "api/info 暴露 net_resets": '"net_resets": net_resets' in src,
        # 冷启动冷却修正的静态特征：初值必须是 None 且判据含 is not None
        "wifi_reassoc_ms 初值 None": "wifi_reassoc_ms = None" in src,
        "冷却判据含 is not None": "if wifi_reassoc_ms is not None and" in src,
    }
    miss = [k for k, v in need.items() if not v]
    check("S0 固件已接入自愈 D（常量/函数/挂钩/诊断字段）", not miss,
          "缺失=%s" % miss if miss else "全部就位")

    # ---------------- 桩：可计数的 WLAN ----------------
    w = net.WLAN(net.STA_IF)
    w.active(True)
    net.sim_set_sta_connected(True)
    calls = {"disconnect": 0, "connect": 0}
    _d, _c = w.disconnect, w.connect

    def _disc():
        calls["disconnect"] += 1
        return _d()

    def _conn(ssid, pwd=None):
        calls["connect"] += 1
        return _c(ssid, pwd)

    w.disconnect = _disc
    w.connect = _conn

    ns["wlan_sta"] = w
    cfg = {"wifi_ssid": "Office-WiFi", "wifi_password": "pw"}
    reassoc = ns["wifi_reassoc_if_dead"]
    # 冷却判定是 ticks_diff(now, wifi_reassoc_ms) < COOLDOWN_MS。为了在需要
    # "绕开冷却" 的用例里确定性地放行，统一用一个远未来的 now。
    # （v6.0.5 修正后初值已是 None，不再需要靠 now_far 去压过 0 这个假时间戳；
    #   S8 专门验证「初值 None 时首次自愈立即触发」。）
    now_far = ns["now_ms"]() + 300000

    # ---------------- S1 未达阈值 ----------------
    ns["mqtt_fail_streak"] = 2
    ns["wifi_reassoc_ms"] = 0
    ns["mqtt_retry"] = 12345
    r1 = reassoc(cfg, now_far)
    check("S1 未达阈值(MQTT_FAIL_REASSOC=3)不动作",
          r1 is False and calls["disconnect"] == 0 and calls["connect"] == 0,
          "r=%s disc=%d conn=%d" % (r1, calls["disconnect"], calls["connect"]))

    # ---------------- S2 达阈值 -> 重新关联 ----------------
    ns["mqtt_fail_streak"] = 3
    ns["wifi_reassoc_ms"] = 0
    ns["mqtt_retry"] = 12345
    r2 = reassoc(cfg, now_far)
    check("S2 达阈值 + WiFi 自称已连接 -> 触发重新关联",
          r2 is True and calls["disconnect"] == 1 and calls["connect"] == 1,
          "r=%s disc=%d conn=%d" % (r2, calls["disconnect"], calls["connect"]))

    # ---------------- S3 触发后状态 ----------------
    check("S3 触发后状态正确（reassocs+1 / fail_streak 清零 / mqtt_retry 归零）",
          ns["wifi_reassocs"] == 1 and ns["mqtt_fail_streak"] == 0
          and ns["mqtt_retry"] == 0,
          "reassocs=%r fails=%r mqtt_retry=%r" % (
              ns["wifi_reassocs"], ns["mqtt_fail_streak"], ns["mqtt_retry"]))

    # ---------------- S4 冷却期 ----------------
    # 距上次触发只过了 ~2s（函数里 sleep 2）<< COOLDOWN，必须被挡住
    ns["mqtt_fail_streak"] = 3
    r4 = reassoc(cfg, ns["now_ms"]())
    check("S4 冷却期内不重复触发",
          r4 is False and calls["disconnect"] == 1,
          "r=%s disc=%d（应仍为 1）" % (r4, calls["disconnect"]))

    # ---------------- S5 WiFi 真断线不插手 ----------------
    net.sim_set_sta_connected(False)
    ns["mqtt_fail_streak"] = 5
    ns["wifi_reassoc_ms"] = 0          # 绕开冷却，确保是「真断线」这一条在拦
    r5 = reassoc(cfg, now_far)
    check("S5 WiFi 真断线(isconnected=False)时不动手",
          r5 is False and calls["disconnect"] == 1,
          "r=%s disc=%d（应仍为 1）" % (r5, calls["disconnect"]))
    net.sim_set_sta_connected(True)

    # ---------------- S6/S7 mqtt_connect 维护失败计数 ----------------
    ns["mqtt_fail_streak"] = 0
    bad = dict(cfg)
    bad.update({"mqtt_host": "127.0.0.1", "mqtt_port": 1,
                "device_id": "7ce8b1c1a7fc", "product_id": "relay4_lfx",
                "mqtt_user": "test", "mqtt_password": "123456",
                "topic_mode": "direct"})
    ok6 = ns["mqtt_connect"](bad)
    check("S6 mqtt_connect 失败使 mqtt_fail_streak 递增",
          ok6 is False and ns["mqtt_fail_streak"] == 1,
          "ok=%s streak=%r" % (ok6, ns["mqtt_fail_streak"]))

    # 用假 MQTTClient 让建连「成功」，验证清零
    uq = stubs["umqtt"]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def set_callback(self, cb):
            pass

        def set_last_will(self, *a, **kw):
            pass

        def connect(self, clean_session=True):
            return 0

        def subscribe(self, *a, **kw):
            pass

        def publish(self, *a, **kw):
            return None

    _real_cls = uq.MQTTClient
    _real_pub = ns["publish_property"]
    uq.MQTTClient = _FakeClient
    ns["publish_property"] = lambda _cfg: None   # 本用例只关心建连计数，不测上报
    try:
        ok7 = ns["mqtt_connect"](bad)
        streak7 = ns["mqtt_fail_streak"]
    finally:
        uq.MQTTClient = _real_cls
        ns["publish_property"] = _real_pub
    check("S7 mqtt_connect 成功使 mqtt_fail_streak 清零",
          ok7 is True and streak7 == 0,
          "ok=%s streak=%r" % (ok7, streak7))

    # ---------------- S8 冷启动回归：初值 None 时首次自愈立即触发 ----------------
    # 实板 bug（2026-09-10）：wifi_reassoc_ms 初值曾是 0，被当成真实时间戳，
    # ticks_diff(now, 0) 在开机前 90s 内恒 < COOLDOWN -> 首次自愈被冷却门误挡。
    # 日志证据：`data path dead: 9 consecutive MQTT connect failures`（阈值 3）。
    # 这里故意用「开机后 1 秒」的 now：旧实现在此必然被挡，新实现必须放行。
    ns["mqtt_fail_streak"] = 3
    ns["wifi_reassoc_ms"] = None          # 模拟刚开机、从未触发过自愈
    ns["mqtt_retry"] = 12345
    d_before = calls["disconnect"]
    r8 = reassoc(cfg, 1000)               # now = 开机后 1000ms，远小于 90s 冷却
    check("S8 冷启动(wifi_reassoc_ms=None)首次自愈立即触发，不被冷却门误挡",
          r8 is True and calls["disconnect"] == d_before + 1,
          "r=%s disc=%d->%d（旧实现在此会 r=False）"
          % (r8, d_before, calls["disconnect"]))

    # ---------------- S9 自愈 E：网络彻底不可用 -> 硬复位阶梯 ----------------
    track = ns["net_dead_track"]
    hopeless = ns["net_reset_if_hopeless"]

    # 9a: MQTT 可用 -> 不可用时长为 0
    ok9a = track(50000, True) == 0 and ns["net_dead_since"] == 0

    # 9b: 冷启动从未连上过 -> 不追踪（那是 portal 失败风暴守卫的活，不能硬复位）
    ns["_sta_connected_once"] = False
    ns["net_dead_since"] = 0
    ok9b = track(50000, False) == 0 and ns["net_dead_since"] == 0

    # 9c: 曾连上过 + MQTT 不可用 -> 开始计时并返回不可用秒数
    ns["_sta_connected_once"] = True
    ns["net_dead_since"] = 0
    s9c = track(100000, False)
    ok9c = s9c == 0 and ns["net_dead_since"] == 100000

    # 9d: 时长不够（180s 阈值）-> 不复位
    ns["wifi_reassocs"] = 5
    ns["net_dead_base_reassocs"] = 0
    ns["mqtt_fail_streak"] = 7
    ok9d = hopeless(cfg, 100000 + 60000) is False

    # 9e: 时长够了但本轮重新关联次数不足 -> 仍不复位
    ns["net_dead_base_reassocs"] = 4      # 本轮只捅了 1 次 < NET_RESET_MIN_REASSOCS(2)
    ok9e = hopeless(cfg, 100000 + 200000) is False

    # 9f: 两者都满足 -> 调 reset() 硬复位
    hits = {"n": 0}
    ns["net_dead_base_reassocs"] = 0      # 本轮捅了 5 次
    ns["net_resets"] = 0
    ns["reset"] = lambda: hits.__setitem__("n", hits["n"] + 1)
    ok9f = hopeless(cfg, 100000 + 200000) is True and hits["n"] == 1

    check("S9 自愈 E 阶梯（可用归零/冷启动豁免/计时/时长不够/次数不够/触发复位）",
          ok9a and ok9b and ok9c and ok9d and ok9e and ok9f,
          "a=%s b=%s c=%s d=%s e=%s f=%s" % (ok9a, ok9b, ok9c, ok9d, ok9e, ok9f))

    # ---------------- 汇总 ----------------
    n_pass = sum(1 for _, ok, _ in CHECKS if ok)
    print()
    print("===== v6.0.5 自愈 D 验收：%d/%d %s =====" % (
        n_pass, len(CHECKS), "PASS" if n_pass == len(CHECKS) else "FAIL"))
    for name, ok, d in CHECKS:
        if not ok:
            print("  FAILED: %s  %s" % (name, d))
    return 0 if n_pass == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
