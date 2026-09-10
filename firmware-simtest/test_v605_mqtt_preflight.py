# -*- coding: utf-8 -*-
"""test_v605_mqtt_preflight.py —— v6.0.5 MQTT 建连超时预检验收

被测固件：esp32-relay4-modbus-gateway/main.py
后端：无需 broker / 无需硬件（只 exec 固件定义 + 本地起一个 TCP 监听做正例）。

背景（2026-09-10 实板实测）：
  链路差时 umqtt 内部的**阻塞** socket.connect() 要等 lwIP SYN 重传耗尽才返回，
  单次可达 ~55s（实测 210s 只跑完 3 次建连尝试）。这段时间主循环完全停摆：
  http_poll() 不执行 -> HTTP :80 无响应、按键不处理、上报停摆。
  这直接把 v6.0.5「HTTP 搬回主循环」的收益吃掉。

修复：建连前先做带 settimeout 的 TCP 可达性预检（_tcp_preflight）；预检失败即记
一次建连失败并立刻返回，主循环继续 http_poll()，RETRY_S 后再试。

断言：
  P0 前置：常量 / 函数 / mqtt_connect 内的调用 / 文档说明 均已就位
  P1 预检正例：对活着的本地监听 -> True（且不误伤）
  P2 预检反例：端口无人监听 -> False，且快速返回（非阻塞）
  P3 预检黑洞地址 -> False，且耗时被 超时 兜住（这是本修复的核心回归点）
  P4 mqtt_connect 对黑洞地址：返回 False 且总耗时 <= 超时 + 余量（旧行为 ~55s）
  P5 mqtt_connect 对黑洞地址：仍正确维护 mqtt_fail_streak / mqtt_retry
  P6 预检通过时 mqtt_connect 正常走到 umqtt 建连（用假 client 验证不被误拦）

用法：
    python firmware-simtest/test_v605_mqtt_preflight.py
"""
from __future__ import annotations

import os
import socket as real_socket
import sys
import threading
import time as real_time

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, PROJ_ROOT)

import run_sim_modbus as R  # noqa: E402  复用桩注入

FW = os.path.join(R.FW_DIR, "main.py")
WEB_PORT = 18097
BOOT_MARK = "\ntry:\n    main()\nexcept Exception as e:"

BLACKHOLE = "10.255.255.1"      # 不可路由：连它会一直重传直到超时
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  |  " + detail) if detail else ""), flush=True)


def load_fw_defs():
    with open(FW, "r", encoding="utf-8") as f:
        src = f.read()
    src = src.replace("\r\n", "\n")
    if BOOT_MARK not in src:
        raise RuntimeError("找不到 main.py 末尾启动块，无法安全 exec")
    src = src.split(BOOT_MARK)[0]
    ns = {"__name__": "_probe"}
    exec(compile(src, FW, "exec"), ns)
    return src, ns


def free_port_listener():
    """起一个本地 TCP 监听做预检正例，返回 (port, stop_fn)。"""
    srv = real_socket.socket(real_socket.AF_INET, real_socket.SOCK_STREAM)
    srv.setsockopt(real_socket.SOL_SOCKET, real_socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def accept_loop():
        while True:
            try:
                c, _ = srv.accept()
                c.close()
            except Exception:
                return

    threading.Thread(target=accept_loop, daemon=True).start()

    def stop():
        try:
            srv.close()
        except Exception:
            pass

    return port, stop


def main():
    print("=" * 78)
    print("v6.0.5 MQTT 建连超时预检验收：别让阻塞 connect 饿死主循环")
    print("=" * 78)

    stubs = R.inject_stubs(web_port=WEB_PORT)
    src, ns = load_fw_defs()

    # ---------------- P0 静态前置 ----------------
    # 注：原「文档说明」断言要求源码里出现一整串人造字面量
    # `MQTT_CONNECT_TIMEOUT_S / _tcp_preflight`，太脆 —— 谁精简一下注释就误报
    # （2026-09-10 实际踩到）。改成**语义检查**：常量定义上方必须有解释性注释，
    # 且该注释要讲清这是「预检」，否则这个超时值就成了无来由的魔法数字。
    _lines = src.splitlines()
    _ci = next((i for i, ln in enumerate(_lines)
                if ln.startswith("MQTT_CONNECT_TIMEOUT_S =")), -1)
    _docwin = _lines[max(0, _ci - 5):_ci] if _ci > 0 else []
    _doc_ok = bool(_docwin) and any(
        ln.lstrip().startswith("#") and "预检" in ln for ln in _docwin)

    need = {
        "常量 MQTT_CONNECT_TIMEOUT_S": "MQTT_CONNECT_TIMEOUT_S =" in src,
        "函数 _tcp_preflight": "def _tcp_preflight(" in src,
        "mqtt_connect 调用预检": "_tcp_preflight(cfg[\"mqtt_host\"]" in src,
        "预检失败走异常记账": '"preflight: tcp' in src,
        "文档说明（常量上方有『预检』注释）": _doc_ok,
    }
    miss = [k for k, v in need.items() if not v]
    check("P0 固件已接入建连超时预检（常量/函数/调用/文档）", not miss,
          "缺失=%s" % miss if miss else "全部就位")
    if miss:
        for k, v in need.items():
            print("        %-28s %s" % (k, "OK" if v else "MISSING"))

    timeout_s = ns.get("MQTT_CONNECT_TIMEOUT_S")
    pre = ns.get("_tcp_preflight")
    check("P0b MQTT_CONNECT_TIMEOUT_S 是有限正数", isinstance(timeout_s, (int, float))
          and 0 < timeout_s <= 10, "值=%r" % (timeout_s,))
    if pre is None:
        print("!! 没有 _tcp_preflight，后续用例无法执行")
        return 1

    # ---------------- P1 预检正例 ----------------
    port, stop = free_port_listener()
    t0 = real_time.time()
    ok1 = pre("127.0.0.1", port, 3.0)
    d1 = real_time.time() - t0
    check("P1 预检正例：活着的本地监听 -> True", ok1 is True,
          "ok=%s 耗时 %.2fs port=%d" % (ok1, d1, port))
    stop()

    # ---------------- P2 预检反例（有界返回） ----------------
    t0 = real_time.time()
    ok2 = pre("127.0.0.1", port, 3.0)          # 监听已关，端口无人
    d2 = real_time.time() - t0
    # 注意：Windows 对「刚关闭的 loopback 端口」不保证立刻 RST，可能重传到超时，
    # 所以这里只断言「失败 + 有界」，不与本机 OS 的 RST 行为绑死。
    check("P2 预检反例：端口无人监听 -> False 且有界返回",
          ok2 is False and d2 <= 3.0 + 1.5, "ok=%s 耗时 %.2fs" % (ok2, d2))

    # ---------------- P3 黑洞地址（核心回归点） ----------------
    t0 = real_time.time()
    ok3 = pre(BLACKHOLE, 9783, timeout_s)
    d3 = real_time.time() - t0
    bounded = d3 <= float(timeout_s) + 1.5
    check("P3 预检黑洞地址 -> False 且被超时兜住（旧行为会阻塞数十秒）",
          ok3 is False and bounded,
          "ok=%s 耗时 %.2fs（上限 %.1fs+1.5）" % (ok3, d3, float(timeout_s)))

    # ---------------- P4/P5 mqtt_connect 对黑洞的耗时与记账 ----------------
    cfg = {"wifi_ssid": "x", "wifi_password": "y", "mqtt_host": BLACKHOLE,
           "mqtt_port": 9783, "device_id": "7ce8b1c1a7fc",
           "product_id": "relay4_lfx", "mqtt_user": "test",
           "mqtt_password": "123456", "topic_mode": "direct"}
    ns["mqtt_fail_streak"] = 0
    ns["mqtt_retry"] = 0
    t0 = real_time.time()
    ok4 = ns["mqtt_connect"](cfg)
    d4 = real_time.time() - t0
    check("P4 mqtt_connect 对黑洞地址：False 且总耗时被超时兜住",
          ok4 is False and d4 <= float(timeout_s) + 1.5,
          "ok=%s 耗时 %.2fs（旧行为实测 ~55s）" % (ok4, d4))

    retry_set = ns["mqtt_retry"] != 0
    check("P5 mqtt_connect 预检失败仍正确记账（streak+1 / mqtt_retry 已排期）",
          ns["mqtt_fail_streak"] == 1 and retry_set,
          "streak=%r mqtt_retry=%r" % (ns["mqtt_fail_streak"], ns["mqtt_retry"]))

    # ---------------- P6 预检通过时不误拦 ----------------
    uq = stubs["umqtt"]
    good_cfg = dict(cfg)
    good_cfg["mqtt_host"] = "127.0.0.1"
    good_cfg["mqtt_port"] = port
    port2, stop2 = free_port_listener()
    good_cfg["mqtt_port"] = port2

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
    ns["publish_property"] = lambda _cfg: None
    ns["mqtt_fail_streak"] = 2
    try:
        ok6 = ns["mqtt_connect"](good_cfg)
        streak6 = ns["mqtt_fail_streak"]
    finally:
        uq.MQTTClient = _real_cls
        ns["publish_property"] = _real_pub
        stop2()
    check("P6 预检通过时不误拦（正常走到 umqtt 建连并清零 fail_streak）",
          ok6 is True and streak6 == 0,
          "ok=%s streak=%r" % (ok6, streak6))

    # ---------------- 汇总 ----------------
    n_pass = sum(1 for _, ok, _ in CHECKS if ok)
    print()
    print("===== v6.0.5 MQTT 建连超时预检验收：%d/%d %s =====" % (
        n_pass, len(CHECKS), "PASS" if n_pass == len(CHECKS) else "FAIL"))
    for name, ok, d in CHECKS:
        if not ok:
            print("  FAILED: %s  %s" % (name, d))
    return 0 if n_pass == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
