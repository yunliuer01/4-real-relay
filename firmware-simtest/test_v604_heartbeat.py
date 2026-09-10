# -*- coding: utf-8 -*-
"""test_v604_heartbeat.py —— v6.0.4 MQTT 链路自愈 + PUBACK 死锁修复 仿真验收

被测固件：esp32-relay4-modbus-gateway/main.py
后端：loopback（无需真实 broker、无需硬件），复用 run_sim_modbus 的桩与 Runner。

背景（2026-09-10 实板事故，三个独立缺陷）：
  1) PUBACK 嵌套死锁（真根因）：umqtt.simple 的 publish(qos=1) 会自旋 wait_msg()
     等 PUBACK，而 wait_msg() 同时负责派发订阅回调。于是「主循环 publish 等
     PUBACK」期间到达的下行会在回调里再 publish(qos=1)，嵌套的 wait_msg 把
     外层那颗 PUBACK 读走丢弃 -> 外层回到阻塞读，永久卡死、不抛异常。
     症状极具迷惑性：心跳照常、/api/info 全绿、属性流彻底停摆、HTTP 全超时。
     修法：PUB_QOS=0（出站一律 qos=0，不等 PUBACK）；订阅侧仍 qos=1。
  2) HTTP 线程直调 publish：/api/relay 在 HTTP 线程里直接 publish_property，
     跨线程操作 MQTT socket。修法：只置 report_requested 标志，主循环消费。
  3) 静默上行丢失自愈：bridge 在网关心跳 payload 里回传 stall_s（父设备属性
     停滞秒数），板子超阈值即主动断开重连（带连接宽限期，防「连上-重连」抖动）。

场景与断言：
  0  仿真桩自检        —— 证明环回桩真能复现 PUBACK 死锁（负数验证，防断言空转）
  A  上线与首包上报
  B  心跳应答
  C  心跳保活不误伤    + 出站 qos 全为 0（PUB_QOS 回归保护，确定性断言）
  D  HTTP 并发打桩     —— 属性上报只由主循环线程发起（HTTP 线程绝不碰 socket）
  E  心跳超时自愈
  F  重连后链路恢复
  G  stall_s 宽限期    —— 刚重连时不因 bridge 报停滞立刻再断（防抖动）
  H  stall_s 触发重连  —— 连接稳定后 bridge 报停滞 -> 主动断开重连
  最后：全程零 PUBACK 死锁事件

用法：
    python firmware-simtest/test_v604_heartbeat.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time as real_time

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, PROJ_ROOT)

import run_sim_modbus as R  # noqa: E402  复用桩注入 / Runner / LoopBackBackend

FLASH_DIR = os.path.join(SIM_DIR, "flash_v604")
WEB_PORT = 18094
HB_TIMEOUT_S = 6            # 板子心跳超时（测试用短值，写进 config.json）
PING_INTERVAL_S = 2         # 测试侧下发的「网关心跳」周期
STALL_RECONNECT_S = 12      # 板子 stall 阈值（测试用短值；固件下限 10s）
CONN_GRACE_S = 30           # 固件常量 CONN_GRACE_S，G/H 场景据此设计
STALL_BIG = 999             # 远超阈值的 stall_s（模拟 bridge 报「父设备很久没上行」）

PRODUCT_ID = R.PRODUCT_ID   # relay4_lfx
DEVICE_ID = R.DEVICE_ID     # SIM-MB-01
BASE = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)
HB_CMD_TOPIC = BASE + "/service/cmd"
HB_ACK_TOPIC = BASE + "/event/hb_ack"
PROP_TOPIC = BASE + "/property/post"

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


def send_hb(backend, msg_id, stall_s=0):
    backend.downlink(HB_CMD_TOPIC, json.dumps({
        "messageId": msg_id,
        "functionId": "__hb__",
        "inputs": [],
        "stall_s": stall_s,
    }))


class HeartbeatPinger(threading.Thread):
    """模拟 bridge 的网关心跳线程：周期下发 functionId=__hb__ + stall_s。"""

    def __init__(self, backend, interval_s, stall_s=0):
        super().__init__(daemon=True)
        self.backend = backend
        self.interval_s = interval_s
        self.stall_s = stall_s
        self.stop_evt = threading.Event()
        self.sent = 0

    def run(self):
        while not self.stop_evt.is_set():
            self.sent += 1
            send_hb(self.backend, "hb-%d" % self.sent, self.stall_s)
            self.stop_evt.wait(self.interval_s)

    def stop(self):
        self.stop_evt.set()


# -------------------- 场景 0：桩自检（负数验证） --------------------
def selfcheck_puback_stub(uq):
    """证明环回桩真能复现 PUBACK 嵌套死锁，否则后面的「零死锁」断言就是空转。"""
    c = uq.MQTTClient(b"v604-selfcheck", "127.0.0.1", 1883)
    try:
        c.connect()
    except Exception as e:
        return False, "selfcheck client connect 失败: %r" % (e,)
    c.subscribe(b"v604/selfcheck/cmd")

    nested = {"n": 0}

    def cb(topic, payload):
        # 模拟 handle_command -> publish_heartbeat_ack：在回调里再发 qos=1
        nested["n"] += 1
        c.publish(b"v604/selfcheck/nested", b"{}", qos=1)

    c.set_callback(cb)

    # --- 0a 负例：qos=1 外层 + 下行同时到达 -> 必须复现死锁 ---
    uq.sim_downlink("v604/selfcheck/cmd", '{"functionId":"__hb__"}')
    deadlocked = False
    err = ""
    try:
        c.publish(b"v604/selfcheck/outer", b"{}", qos=1)
    except uq.MQTTPubackDeadlock as e:
        deadlocked = True
        err = str(e)[:80]
    except Exception as e:  # 其它异常也算失败
        err = "非预期异常: %r" % (e,)

    # --- 0b 正例：qos=0 即使下行在队列里也绝不进入自旋 ---
    uq.sim_downlink("v604/selfcheck/cmd", '{"functionId":"__hb__"}')
    ok0 = True
    err0 = ""
    try:
        c.publish(b"v604/selfcheck/outer0", b"{}", qos=0)
    except Exception as e:
        ok0 = False
        err0 = "qos=0 不应抛异常: %r" % (e,)

    c.disconnect()
    detail = ("qos=1 复现死锁=%s(nested=%d, %s) / qos=0 正常=%s%s"
              % (deadlocked, nested["n"], err, ok0, (" " + err0) if err0 else ""))
    return (deadlocked and ok0), detail


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

    print("[SIM] 启动 v6.0.4 验收：hb_timeout_s=%ds ping=%ds stall_reconnect_s=%ds"
          % (HB_TIMEOUT_S, PING_INTERVAL_S, STALL_RECONNECT_S), flush=True)

    stubs = R.inject_stubs(web_port=WEB_PORT)
    uq = stubs["umqtt"]
    backend = R.LoopBackBackend(uq)

    # ---- 场景 0：先验证桩本身够狠（在启动固件前做，避免污染记录）----
    ok0, d0 = selfcheck_puback_stub(uq)
    check("0 环回桩可复现 PUBACK 嵌套死锁（负数验证）", ok0, d0)
    if not ok0:
        print("[abort] 桩自检失败：后续「零死锁」断言不可信，直接终止", flush=True)
        uq.sim_set_loopback(False)
        return 2
    uq.sim_reset_records()

    runner = R.FirmwareRunner(os.path.join(R.FW_DIR, "main.py"))
    runner.start()

    # 立刻开始打心跳，避免场景 A 期间被 watchdog 误判超时
    pinger = HeartbeatPinger(backend, PING_INTERVAL_S, stall_s=0)
    pinger.start()

    try:
        # ---- A 上线与首包上报 ----
        ok_online = wait_until(lambda: any(
            t == BASE + "/online" for t, _ in uq.sim_recv_msgs()), 25)
        check("A1 上线发布 /online", ok_online, BASE + "/online")

        prop = backend.wait_property(timeout_s=25)
        check("A2 属性上报 /property/post",
              bool(prop) and prop.get("deviceId") == DEVICE_ID,
              json.dumps(prop, ensure_ascii=False)[:160] if prop else "timeout")

        # ---- B 心跳应答 ----
        idx = uq.sim_msg_count()
        send_hb(backend, "hb-seq-check", stall_s=0)
        tp, pl = backend.wait_topic(HB_ACK_TOPIC, timeout_s=10, from_index=idx)
        seq = ""
        if pl:
            try:
                seq = json.loads(pl).get("data", {}).get("seq", "")
            except Exception:
                pass
        check("B1 收到 __hb__ 后回 /event/hb_ack",
              tp is not None and seq == "hb-seq-check",
              "topic=%s seq=%r" % (tp, seq))

        # ---- C 心跳保活期间不重连 + 出站 qos 必须全 0 ----
        conn0 = uq.sim_conn_count()
        sent0 = pinger.sent
        real_time.sleep(HB_TIMEOUT_S * 3)
        conn1 = uq.sim_conn_count()
        n_hb = pinger.sent - sent0
        check("C1 心跳按时到达时绝不重连", conn1 == conn0,
              "conn %d->%d，期间心跳 %d 次" % (conn0, conn1, n_hb))
        n_ack = sum(1 for t, _ in uq.sim_recv_msgs() if t == HB_ACK_TOPIC)
        check("C2 心跳均被应答（双向链路活着）", n_ack >= n_hb,
              "ack=%d hb=%d" % (n_ack, n_hb))
        qoses = uq.sim_outbound_qos()
        bad_q = sorted(set(q for q in qoses if q != 0))
        check("C3 出站 publish 全部 qos=0（PUBACK 死锁的确定性保护）", not bad_q,
              "出站 %d 条，非 0 qos=%s" % (len(qoses), bad_q))

        # ---- D HTTP 并发打桩：上报只能由主循环线程发起 ----
        meta = uq.sim_recv_meta()
        tids_prop = set(m["tid"] for m in meta if "property/post" in m["topic"])
        tid_main = next(iter(tids_prop)) if len(tids_prop) == 1 else None
        check("D1 属性上报来自唯一线程（主循环）", tid_main is not None,
              "property/post 线程集合=%s" % sorted(tids_prop))

        d_idx = uq.sim_msg_count()
        d_meta_idx = len(meta)
        paths = ["/api/relay?ch=%d&state=%d" % (c, s)
                 for c in (1, 2, 3, 4) for s in (0, 1)]
        paths += ["/api/relay/all?state=%d" % s for s in (1, 0)]
        paths *= 3   # 30 个并发请求，复现实板「高频 /api/relay 打死属性流」
        http_errs = []

        def fire(p):
            try:
                r = R.http_request(WEB_PORT, p, timeout=10)
                if not r or b"200 OK" not in r:
                    http_errs.append("%s -> %r" % (p, (r or b"")[:60]))
            except Exception as e:
                http_errs.append("%s -> %r" % (p, e))

        ths = [threading.Thread(target=fire, args=(p,)) for p in paths]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=20)
        check("D2 HTTP 打桩请求全部成功", not http_errs,
              "%d/%d 失败 %s" % (len(http_errs), len(paths), http_errs[:2]))

        got_report = wait_until(
            lambda: any("property/post" in m["topic"]
                        for m in uq.sim_recv_meta()[d_meta_idx:]), 15)
        check("D3 打桩后主循环完成补报（report_requested 生效）", got_report,
              "property/post 增量=%d" % sum(
                  1 for m in uq.sim_recv_meta()[d_meta_idx:]
                  if "property/post" in m["topic"]))

        after_tids = set(m["tid"] for m in uq.sim_recv_meta()[d_meta_idx:])
        check("D4 HTTP 线程绝不直接 publish（跨线程 socket 已消除）",
              after_tids and after_tids == {tid_main},
              "打桩期间出站线程集合=%s（主循环 tid=%s）" % (sorted(after_tids), tid_main))
        check("D5 打桩期间零 PUBACK 死锁（实板事故的直接回归）",
              uq.sim_deadlock_count() == 0,
              "deadlocks=%d %s" % (uq.sim_deadlock_count(),
                                   uq.sim_deadlock_events()[:1]))
        check("D6 打桩后连接未被打断", uq.sim_conn_count() == conn1,
              "conn %d->%d" % (conn1, uq.sim_conn_count()))

        # ---- E 心跳超时 -> 主动重连 ----
        pinger.stop()
        conn2 = uq.sim_conn_count()
        got_log = wait_until(lambda: "gateway heartbeat timeout" in lb.text(),
                             HB_TIMEOUT_S * 3 + 15)
        check("E1 心跳超时触发 watchdog 日志", got_log,
              "期望日志含 'gateway heartbeat timeout(%ds)'" % HB_TIMEOUT_S)
        reconnected = wait_until(lambda: uq.sim_conn_count() > conn2, 20)
        check("E2 心跳超时后主动断开重连（无需人工 probe）", reconnected,
              "conn %d->%d" % (conn2, uq.sim_conn_count()))
        t_conn = real_time.time()

        # ---- F 重连后链路恢复 ----
        pinger = HeartbeatPinger(backend, PING_INTERVAL_S, stall_s=0)
        pinger.start()
        idx = uq.sim_msg_count()
        send_hb(backend, "hb-post-reconnect", stall_s=0)
        tp, pl = backend.wait_topic(HB_ACK_TOPIC, timeout_s=15, from_index=idx)
        check("F1 重连后新会话继续应答心跳", tp is not None, "topic=%s" % tp)

        # ---- G stall_s 宽限期：刚重连时不立刻再断（防「连上-重连」抖动）----
        conn3 = uq.sim_conn_count()
        g_pos = lb.pos()
        send_hb(backend, "hb-stall-grace", stall_s=STALL_BIG)
        deferred = wait_until(lambda: "deferred" in lb.text()[g_pos:], 8)
        check("G1 宽限期内 stall 指令被延后（日志 deferred）", deferred,
              "conn_age<CONN_GRACE_S(=%ds) 时应打印 deferred" % CONN_GRACE_S)
        real_time.sleep(6)
        check("G2 宽限期内不因 stall_s 断开重连", uq.sim_conn_count() == conn3,
              "conn %d->%d" % (conn3, uq.sim_conn_count()))

        # ---- H stall_s 触发重连：连接稳定后 bridge 报停滞即自愈 ----
        wait_until(lambda: real_time.time() - t_conn >= CONN_GRACE_S + 6, 45)
        conn4 = uq.sim_conn_count()
        send_hb(backend, "hb-stall-fire", stall_s=STALL_BIG)
        fired = wait_until(lambda: uq.sim_conn_count() > conn4, 25)
        check("H1 连接稳定后 bridge 报停滞 -> 主动断开重连", fired,
              "conn %d->%d（conn_age≥%ds 且已 ACK≥2 次）"
              % (conn4, uq.sim_conn_count(), CONN_GRACE_S))

        check("Z1 全程零 PUBACK 死锁事件", uq.sim_deadlock_count() == 0,
              "deadlocks=%d" % uq.sim_deadlock_count())
    finally:
        pinger.stop()
        runner.stop()
        backend.close()

    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_all = len(RESULTS)
    print("\n===== v6.0.4 PUBACK 死锁修复 + 自愈验收：%d/%d %s ====="
          % (n_pass, n_all, "PASS" if n_pass == n_all else "FAIL"), flush=True)
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAILED: %s  %s" % (name, detail), flush=True)
    return 0 if n_pass == n_all else 1


if __name__ == "__main__":
    sys.exit(main())
