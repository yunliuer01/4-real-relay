# -*- coding: utf-8 -*-
"""test_v605_http_poll.py —— v6.0.5 HTTP 服务主循环化（非阻塞轮询）仿真验收

被测固件：esp32-relay4-modbus-gateway/main.py
后端：loopback（无需真实 broker、无需硬件），复用 run_sim_modbus 的桩与 Runner。

背景（2026-09-10 实板事故 #2，独立于 v6.0.4 的 PUBACK 死锁）：
  /api/relay 并发 >= 4 时整机**静默冻结** —— ARP 不答、串口输出直接停住、
  无 panic / 无复位 / 无异常日志，只能 esptool 硬复位，复位后自愈。
  修复前后表现一致，与 PUB_QOS 无关。

根因（HTTP 独立线程模型的两个叠加风险）：
  1) HTTP 线程与主循环并发操作 lwIP（accept/recv vs MQTT socket / Modbus TCP），
     宏观表现为「冻结」而非崩溃 —— 与本项目既有的 lwIP 跨线程脆弱性同源；
  2) C3 内部 RAM 只允许 4KB 线程栈（8192/6144 都 can't create thread），
     请求突发时栈余量极小。
  单线程串行 accept 也解释了为什么是「并发 >= 4」而不是单个请求才触发。

修复（v6.0.5）：
  HTTP 服务回到主线程 —— 非阻塞监听 socket + 主循环 http_poll() 单连接状态机。
  accept/recv 全非阻塞；首字节 1s 超时、整包 3s 超时、8KB 缓冲上限；单轮最多
  处理 4 个「已就绪」连接。所有 socket 操作只剩主线程一个执行者。

场景与断言：
  P0  前置：新固件已加载（源码无 HTTP 线程 + /api/info 暴露 v6.0.5 诊断字段）
  P1  基线单请求 200
  P2  并发 16 全部 200
  P3  并发 32 全部 200
  P4  并发洪泛期间属性流未断 + 主循环未被 HTTP 饿死（最大上报间隔有界）
  P5  半开连接（只连不发）1s 内被丢弃，且不拖死后续正常请求
  P6  分片请求（先发半截 head，200ms 后补完）仍 200
  P7  超长无终止请求（>8KB 无 \\r\\n\\r\\n）被丢弃且服务不崩
  P8  全程零 PUBACK 死锁、零异常重连
  P9  http_reqs 计数与成功请求数一致（证明确实走的主循环轮询路径）

用法：
    python firmware-simtest/test_v605_http_poll.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time as real_time

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, PROJ_ROOT)

import run_sim_modbus as R  # noqa: E402  复用桩注入 / Runner / LoopBackBackend

FLASH_DIR = os.path.join(SIM_DIR, "flash_v605")
WEB_PORT = 18095
PING_INTERVAL_S = 2          # 测试侧下发「网关心跳」的周期（保活，避免 watchdog 误判）
HB_TIMEOUT_S = 60            # 板子心跳超时（放宽，本轮不测自愈）
STALL_RECONNECT_S = 99999    # 停滞阈值拉到极大：本轮不触发 force_reconnect

PRODUCT_ID = R.PRODUCT_ID
DEVICE_ID = R.DEVICE_ID
BASE = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)
HB_CMD_TOPIC = BASE + "/service/cmd"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  |  " + detail) if detail else ""), flush=True)
    return bool(ok)


def wait_until(fn, timeout_s, step=0.25):
    t0 = real_time.time()
    while real_time.time() - t0 < timeout_s:
        if fn():
            return True
        real_time.sleep(step)
    return False


def get_info():
    resp = R.http_request(WEB_PORT, "/api/info", timeout=8)
    if not resp or b"200 OK" not in resp:
        return None
    body = resp.split(b"\r\n\r\n", 1)[-1]
    try:
        return json.loads(body)
    except Exception:
        return None


def recv_all(s, timeout=8):
    """读到对端关闭为止。"""
    s.settimeout(timeout)
    chunks = []
    while True:
        try:
            d = s.recv(4096)
        except Exception:
            break
        if not d:
            break
        chunks.append(d)
    return b"".join(chunks)


class HeartbeatPinger(threading.Thread):
    """模拟 bridge 的网关心跳线程：周期下发 functionId=__hb__ + stall_s=0。"""

    def __init__(self, backend, interval_s):
        super().__init__(daemon=True)
        self.backend = backend
        self.interval_s = interval_s
        self.stop_evt = threading.Event()
        self.sent = 0

    def run(self):
        while not self.stop_evt.is_set():
            self.sent += 1
            self.backend.downlink(HB_CMD_TOPIC, json.dumps({
                "messageId": "hb-%d" % self.sent,
                "functionId": "__hb__",
                "inputs": [],
                "stall_s": 0,
            }))
            self.stop_evt.wait(self.interval_s)

    def stop(self):
        self.stop_evt.set()


def burst(paths):
    """并发发起 N 个 GET，返回失败列表。"""
    errs = []

    def fire(p):
        try:
            r = R.http_request(WEB_PORT, p, timeout=15)
            if not r or b"200 OK" not in r:
                errs.append("%s -> %r" % (p, (r or b"")[:50]))
        except Exception as e:
            errs.append("%s -> %r" % (p, e))

    ths = [threading.Thread(target=fire, args=(p,)) for p in paths]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    return errs


def count_props(uq):
    return sum(1 for t, _ in uq.sim_recv_msgs() if "property/post" in t)


def main():
    lb = R.LogBuffer(sys.stdout)
    sys.stdout = lb

    os.makedirs(FLASH_DIR, exist_ok=True)
    os.chdir(FLASH_DIR)
    for name in ("config.json",):
        try:
            os.remove(name)
        except OSError:
            pass
    try:
        os.remove("/portal.flag")
    except OSError:
        pass

    cfg = {
        "wifi_ssid": "sim-wifi",
        "wifi_password": "12345678",
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 1883,
        "mqtt_user": "test",
        "mqtt_password": "123456",
        "product_id": PRODUCT_ID,
        "device_id": DEVICE_ID,
        "report_interval": 1,
        "topic_mode": "direct",
        "modbus": {"enabled": False},
        "hb_timeout_s": HB_TIMEOUT_S,
        "stall_reconnect_s": STALL_RECONNECT_S,
    }
    with open("config.json", "w") as f:
        json.dump(cfg, f)

    print("[SIM] 启动 v6.0.5 验收：HTTP 主循环非阻塞轮询（web_port=%d）" % WEB_PORT,
          flush=True)

    # ---- P0 前置：静态确认固件已换成主循环模型 ----
    fw_src = open(os.path.join(R.FW_DIR, "main.py"), "r", encoding="utf-8").read()
    no_http_thread = ("start_new_thread(_serve" not in fw_src
                      and "def _serve()" not in fw_src)
    has_poll = ("def http_poll(" in fw_src
                and "HTTP_MAX_CONN_PER_POLL" in fw_src
                and "def stop_control_http(" in fw_src)
    check("P0a 固件已移除 HTTP 独立线程、新增主循环 http_poll", no_http_thread and has_poll,
          "no_thread=%s has_poll=%s" % (no_http_thread, has_poll))

    stubs = R.inject_stubs(web_port=WEB_PORT)
    uq = stubs["umqtt"]
    backend = R.LoopBackBackend(uq)

    runner = R.FirmwareRunner(os.path.join(R.FW_DIR, "main.py"))
    runner.start()
    pinger = HeartbeatPinger(backend, PING_INTERVAL_S)
    pinger.start()

    try:
        # ---- P0b 上线 + 新诊断字段 ----
        ok_online = wait_until(lambda: any(
            t == BASE + "/online" for t, _ in uq.sim_recv_msgs()), 30)
        check("P0b 固件上线发布 /online", ok_online, BASE + "/online")

        info0 = None
        for _ in range(20):
            info0 = get_info()
            if info0:
                break
            real_time.sleep(0.5)
        has_fields = bool(info0) and all(
            k in info0 for k in ("http_polls", "http_reqs", "http_aborts", "http_open"))
        check("P0c /api/info 暴露 v6.0.5 HTTP 轮询诊断字段", has_fields,
              "字段=%s" % (sorted(k for k in (info0 or {}) if k.startswith("http"))))
        if not (ok_online and has_fields):
            print("[abort] 前置失败，后续断言无意义", flush=True)
            pinger.stop()
            return 2

        # ---- P1 基线单请求 ----
        r1 = R.http_request(WEB_PORT, "/api/status", timeout=8)
        check("P1 基线单请求 200", bool(r1) and b"200 OK" in r1,
              (r1 or b"")[:40].decode("utf-8", "replace"))

        reqs_base = info0.get("http_reqs", 0)
        conn0 = uq.sim_conn_count()

        # ---- P2 并发 16 ----
        paths16 = ["/api/relay?ch=%d&state=%d" % (c, s)
                   for c in (1, 2, 3, 4) for s in (0, 1)] * 2   # 16 个
        errs16 = burst(paths16)
        check("P2 并发 16 请求全部 200（实板 ≥4 即冻结）", not errs16,
              "失败 %d/16 %s" % (len(errs16), errs16[:2]))

        # ---- P3 并发 32 ----
        paths32 = ["/api/relay?ch=%d&state=%d" % (c, s)
                   for c in (1, 2, 3, 4) for s in (0, 1)] * 4   # 32 个
        prop_before = count_props(uq)
        stop_sample = threading.Event()
        samples = []

        def sampler():
            while not stop_sample.is_set():
                samples.append((real_time.time(), count_props(uq)))
                stop_sample.wait(0.2)

        st = threading.Thread(target=sampler, daemon=True)
        st.start()
        errs32 = burst(paths32)
        real_time.sleep(2.0)
        stop_sample.set()
        st.join(timeout=3)
        check("P3 并发 32 请求全部 200", not errs32,
              "失败 %d/32 %s" % (len(errs32), errs32[:2]))

        # ---- P4 属性流未断 + 主循环未被饿死 ----
        prop_after = count_props(uq)
        check("P4a 并发洪泛后属性流仍在推进", prop_after > prop_before,
              "property/post %d -> %d" % (prop_before, prop_after))

        max_gap = 0.0
        prev_t, prev_n = None, None
        for t, n in samples:
            if prev_n is not None:
                if n == prev_n:
                    max_gap = max(max_gap, t - prev_t)
                else:
                    prev_t, prev_n = t, n
            else:
                prev_t, prev_n = t, n
        # 采样窗口内若某段一直不涨，说明主循环被 HTTP 饿死
        check("P4b 主循环未被 HTTP 饿死（上报间隔有界）", max_gap <= 6.0,
              "最长无新增上报间隔 %.1fs（阈值 6s，采样 %d 次）" % (max_gap, len(samples)))

        # ---- P5 半开连接 ----
        half = []
        try:
            for _ in range(3):
                s = socket.create_connection(("127.0.0.1", WEB_PORT), timeout=5)
                half.append(s)
            t0 = real_time.time()
            r5 = R.http_request(WEB_PORT, "/api/status", timeout=15)
            dt5 = real_time.time() - t0
            check("P5a 半开连接占位时正常请求仍 200", bool(r5) and b"200 OK" in r5,
                  "耗时 %.1fs" % dt5)
            check("P5b 半开连接只带来有限延迟（首字节超时 1s/个）", dt5 < 10.0,
                  "%.1fs（3 个半开 * 1s + 处理）" % dt5)
            closed = 0
            for s in half:
                d = recv_all(s, timeout=8)
                if d == b"":
                    closed += 1
            check("P5c 半开连接被服务端主动关闭（读回 EOF）", closed == 3,
                  "%d/3 收到 EOF" % closed)
        finally:
            for s in half:
                try:
                    s.close()
                except Exception:
                    pass

        # ---- P6 分片请求 ----
        try:
            s6 = socket.create_connection(("127.0.0.1", WEB_PORT), timeout=8)
            s6.sendall(b"GET /api/status HTTP/1.0\r\nHost: x\r\n")
            real_time.sleep(0.2)
            s6.sendall(b"Connection: close\r\n\r\n")
            r6 = recv_all(s6, timeout=8)
            s6.close()
            check("P6 分片请求（半截 head + 200ms 补完）仍 200",
                  b"200 OK" in r6, (r6 or b"")[:40].decode("utf-8", "replace"))
        except Exception as e:
            check("P6 分片请求（半截 head + 200ms 补完）仍 200", False, repr(e))

        # ---- P7 超长无终止请求 ----
        try:
            s7 = socket.create_connection(("127.0.0.1", WEB_PORT), timeout=8)
            s7.sendall(b"GET /api/status HTTP/1.0\r\nX-F: " + b"A" * 12000)
            r7 = recv_all(s7, timeout=10)
            s7.close()
            check("P7a 超长无终止请求被丢弃（连接关闭，未撑爆内存）", r7 == b"",
                  "回包 %d 字节" % len(r7))
            r7b = R.http_request(WEB_PORT, "/api/status", timeout=10)
            check("P7b 超长请求后服务仍健康（下一个请求 200）",
                  bool(r7b) and b"200 OK" in r7b, (r7b or b"")[:40].decode("utf-8", "replace"))
        except Exception as e:
            check("P7a 超长无终止请求被丢弃（连接关闭，未撑爆内存）", False, repr(e))
            check("P7b 超长请求后服务仍健康（下一个请求 200）", False, "跳过")

        # ---- P8 零死锁 / 零异常重连 ----
        check("P8a 全程零 PUBACK 死锁", uq.sim_deadlock_count() == 0,
              "deadlocks=%d" % uq.sim_deadlock_count())
        check("P8b 全程未发生异常重连", uq.sim_conn_count() == conn0,
              "conn %d->%d" % (conn0, uq.sim_conn_count()))
        qoses = uq.sim_outbound_qos()
        bad_q = sorted(set(q for q in qoses if q != 0))
        check("P8c 出站仍全部 qos=0", not bad_q,
              "出站 %d 条，非 0 qos=%s" % (len(qoses), bad_q))

        # ---- P9 请求计数 ----
        info1 = get_info()
        reqs_after = (info1 or {}).get("http_reqs", -1)
        polls = (info1 or {}).get("http_polls", -1)
        check("P9 http_reqs 计数与成功请求数一致（确认走主循环轮询）",
              reqs_after >= reqs_base + 45 and polls > 0,
              "http_reqs %d -> %d（本轮成功请求 ~52），http_polls=%d，aborts=%s"
              % (reqs_base, reqs_after, polls, (info1 or {}).get("http_aborts")))

    finally:
        pinger.stop()
        runner.stop()

    print("\n===== v6.0.5 HTTP 主循环轮询验收：%d/%d PASS ====="
          % (sum(1 for _, ok, _ in RESULTS if ok), len(RESULTS)), flush=True)
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAIL: %s | %s" % (name, detail), flush=True)

    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
