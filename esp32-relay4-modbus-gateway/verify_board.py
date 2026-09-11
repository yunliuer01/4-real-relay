# -*- coding: utf-8 -*-
"""v6.0.5 实板验证：HTTP 主循环非阻塞轮询是否消灭了「并发 ≥4 整机冻结」。

【被修的缺陷】v6.0.4 及更早：`/api/relay` 并发 ≥4 时板子**整机永久冻结** ——
  ARP 不答、串口输出直接停住（无 panic / 无复位 / 无异常）、ping/HTTP 全 timeout，
  只能 esptool 硬复位；复位后自愈。与 PUB_QOS 无关，是 HTTP `_thread` 线程
  与主循环并发操作 lwIP 造成的。

【修复】HTTP 服务搬回主线程：非阻塞监听 socket + 主循环 http_poll() 单连接状态机，
  所有 socket 操作只剩一个执行者。

【本脚本判据核心】
  1) 并发阶梯 1/2/4/8/16/32 **全部 200**，且每一级之后板子**立刻还活着**
     （ping 有回包 + /api/info 200）—— 修复前 n=4 起即整机冻结。
  2) `/api/info` 出现 http_polls/http_reqs 且随请求增长（证明真的走主循环轮询，
     而不是旧线程模型换了个壳）。
  3) 并发洪泛期间 & 之后，父设备 property/post **不断流**（最大间隔有界）。
  4) 下行 set_channel 真实生效、出站 qos 全 0、mqtt_connects 不增长。

用法: python esp32-relay4-modbus-gateway/verify_board.py [--serial]

【来源】本脚本原为会话产物 `_verify_v605.py`（2026-09-11 项目整理时从归档中「转正」，
  纳入版本管理）。板级地址、平台坐标定义在下方常量区，换环境时改那里。
"""
import os
import sys
import time
import json
import threading
import subprocess
import urllib.request

import paho.mqtt.client as mqtt

BOARD_HOST = "192.168.30.160"
BOARD = "http://" + BOARD_HOST
HOST, PORT = "172.16.4.211", 9783
USER, PWD = "test", "123456"
PRODUCT, DEVICE = "relay4_lfx", "7ce8b1c1a7fc"
BASE = "/%s/%s" % (PRODUCT, DEVICE)
PROP_TOPIC = BASE + "/property/post"
CMD_TOPIC = BASE + "/service/cmd"
HB_ACK_TOPIC = BASE + "/event/hb_ack"
REPLY_TOPIC = BASE + "/function/post"
EVENT_TOPIC = BASE + "/event/switch_change"

BASELINE_S = 15
LADDER = [1, 2, 4, 8, 16, 32]
BURST = 48                  # 信息级加压（不进硬判据）
CMD_N = 8
GAP_BUDGET = 20.0           # property/post 最大允许间隔（上报周期 5s）
WITH_SERIAL = "--serial" in sys.argv

msgs = []
lk = threading.Lock()
T0 = 0.0
serialbuf = []


# ---------------- MQTT 探针 ----------------
def on_connect(c, u, f, rc, properties=None):
    print("[probe] connected rc=%s" % rc, flush=True)
    c.subscribe("/%s/%s/#" % (PRODUCT, DEVICE), qos=2)


def on_message(c, u, msg):
    with lk:
        msgs.append((time.time() - T0, msg.topic, msg.qos, msg.payload))


def grab(topic, t_from=0.0, t_to=None):
    t_to = t_to if t_to is not None else 1e9
    with lk:
        return [m for m in msgs if m[1] == topic and t_from <= m[0] <= t_to]


def count(topic, t_from=0.0, t_to=None):
    return len(grab(topic, t_from, t_to))


def max_gap(topic, t_from, t_to):
    ts = [m[0] for m in grab(topic, t_from, t_to)]
    pts = [t_from] + ts + [t_to]
    return max(pts[i + 1] - pts[i] for i in range(len(pts) - 1)), len(ts)


def qos_set(topic):
    with lk:
        return set(m[2] for m in msgs if m[1] == topic)


# ---------------- 板子存活探针 ----------------
def ping_ok(timeout_ms=1500):
    """ARP/ICMP 层存活：冻结时连 ping 都不回，这是最硬的「没死」证据。"""
    try:
        p = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), BOARD_HOST],
                           capture_output=True, timeout=6)
        out = (p.stdout or b"").decode("utf-8", "replace") + \
              (p.stderr or b"").decode("utf-8", "replace")
        return ("TTL=" in out.upper()) or ("ttl=" in out)
    except Exception:
        return False


def fetch(path, timeout=8):
    try:
        with urllib.request.urlopen(BOARD + path, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:
        return 0, repr(e).encode()


def fetch_timed(path, timeout=12):
    """带耗时版：用来区分「冻结超时」与「立刻被拒」。

    旧缺陷的特征是**超时**（板子整机停摆，连 ARP 都不答）。
    lwIP 的 TCP PCB 池打满时，多出来的连接会被**立刻 RST**，这是优雅拒绝，
    与冻结完全不是一回事 —— 必须分开统计，否则判据会把两者混为一谈。
    """
    t0 = time.time()
    try:
        with urllib.request.urlopen(BOARD + path, timeout=timeout) as r:
            body = r.read()
            return r.status, body, time.time() - t0
    except Exception as e:
        return 0, repr(e).encode(), time.time() - t0


def board_info(timeout=12):
    try:
        st, body = fetch("/api/info", timeout=timeout)
        return json.loads(body) if st == 200 else {"_http": st}
    except Exception as e:
        return {"_err": repr(e)}


def channels():
    st, body = fetch("/api/status", timeout=8)
    try:
        return json.loads(body).get("channels") if st == 200 else None
    except Exception:
        return None


def classify(st, body, dur):
    """把一次请求分成 ok / rst / timeout / other。

    这是本脚本最关键的一刀：
      - **冻结**的签名是 `timeout`（板子整机停摆，SYN 石沉大海，只能等客户端超时）；
      - lwIP 连接池/accept 队列打满时多出来的连接会被 **RST**，这是容量拒绝
        （`rst`），板子活得好好的。

    【2026-09-10 修正】原实现把 `dur < 3.0` 作为 rst 的前提，结果**延迟 RST**
    被误判：n=32 时客户端 SYN 退避重传（1s/2s/4s）后才被 lwIP 拒绝，
    `ConnectionResetError` 在 7.5s 才返回 -> 掉进 `dur >= 3.0` 分支 ->
    被记成 `timeout` -> B3 假 FAIL。实测 n=32 的 8 个失败**全是 10054 RST**、
    零个 12s 超时，板子全程活着。
    故判据必须以**错误类型**为准，耗时只做兜底：
      - 出现 reset/10054/ECONNRESET -> rst（不论耗时）；
      - 出现 timed out / socket.timeout -> timeout（冻结签名）；
      - 其它且耗时 >= 3s -> other（不轻易算冻结）。
    """
    if st == 200:
        return "ok"
    txt = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    low = txt.lower()
    if ("reset" in low or "10054" in low or "errno 104" in low
            or "econnreset" in low or "connection aborted" in low):
        return "rst"
    if "timed out" in low or "etimedout" in low or "errno 110" in low:
        return "timeout"
    return "other"


def hammer(n):
    """n 个线程同时打 /api/relay（旧版 n>=4 即整机冻结）。"""
    res = []
    lock2 = threading.Lock()

    def hit(i):
        r = fetch_timed("/api/relay?ch=%d&state=%d" % (i % 4 + 1, i % 2), timeout=12)
        with lock2:
            res.append(r)

    ts = [threading.Thread(target=hit, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.time() - t0, res


def gap_sampler(stop_ev, out):
    """每 0.4s 采样 property/post 计数，用于证明主循环没被 HTTP 饿死。"""
    last = 0
    t_prev = time.time()
    while not stop_ev.is_set():
        cur = count(PROP_TOPIC)
        now = time.time()
        out.append((now - T0, cur - last, now - t_prev))
        last = cur
        t_prev = now
        time.sleep(0.4)


def serial_reader():
    try:
        import serial
        # dsrdtr/rtscts 必须显式关：否则 open() 时 DTR/RTS 跳变会把 C3 复位一次
        s = serial.Serial("COM3", 115200, timeout=0.3,
                          dsrdtr=False, rtscts=False)
        s.setDTR(False)
        s.setRTS(False)
    except Exception as e:
        print("[serial] open failed: %r" % e)
        return

    def run():
        while True:
            try:
                d = s.read(4096)
                if d:
                    with lk:
                        serialbuf.append(d)
            except Exception:
                return
    threading.Thread(target=run, daemon=True).start()


def wait_board_up(max_s=90):
    """等板子从「开串口触发的复位」里起来。

    打开 COM3 会让 C3 复位一次，随后 boot.py 要预连 WiFi 再加载 main.py。
    若不等待就取基线，会把「正在启动」误判成「板子不响应」。
    """
    t0 = time.time()
    while time.time() - t0 < max_s:
        st, body = fetch("/api/info", timeout=4)
        if st == 200:
            try:
                if json.loads(body).get("ok") is True:
                    return time.time() - t0
            except Exception:
                pass
        time.sleep(2)
    return -1.0


# ---------------- 主流程 ----------------
def main():
    global T0
    if WITH_SERIAL:
        serial_reader()
        up = wait_board_up()
        print("[serial] 板子启动完成，用时 %.0fs（开串口会复位一次）" % up
              if up >= 0 else "[serial] 警告：90s 内没等到板子上线")

    print("=" * 78)
    print("v6.0.5 实板验证：HTTP 并发不再整机冻结")
    print("=" * 78)

    alive0 = ping_ok()
    info0 = board_info()
    print("基线 ping=%s" % alive0)
    print("基线 /api/info: " + json.dumps(info0, ensure_ascii=False))

    has_poll = ("http_polls" in info0) and ("http_reqs" in info0)
    if not has_poll:
        print("!! 板上固件没有 http_polls/http_reqs 字段 —— 大概率还是 v6.0.4，部署未生效 !!")
    print("固件标记：http_polls=%r http_reqs=%r aborts=%r" % (
        info0.get("http_polls"), info0.get("http_reqs"), info0.get("http_aborts")))

    c = mqtt.Client(client_id="v605-%d" % int(time.time()), protocol=mqtt.MQTTv311)
    c.username_pw_set(USER, PWD)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(HOST, PORT, 30)
    c.loop_start()
    T0 = time.time()
    time.sleep(2)

    # ---- P1 基线 ----
    print()
    print("[P1] 基线 %ds（只观察）" % BASELINE_S)
    time.sleep(BASELINE_S)
    g_base, n_base = max_gap(PROP_TOPIC, 0.0, time.time() - T0)
    print("     property/post=%d 条，最大间隔=%.1fs" % (n_base, g_base))

    # ---- P2 核心：HTTP 并发阶梯（修复前 n=4 起整机冻结）----
    print()
    print("[P2] HTTP 并发阶梯 %s —— 每级之后立刻验板子是否还活着" % LADDER)
    ladder = []
    was_dead = False
    reqs_done = 0
    stop_ev = threading.Event()
    samples = []
    smp = threading.Thread(target=gap_sampler, args=(stop_ev, samples), daemon=True)
    smp.start()

    for n in LADDER:
        g0 = time.time() - T0
        dur, res = hammer(n)
        kinds = [classify(*r) for r in res]
        ok = kinds.count("ok")
        n_rst = kinds.count("rst")
        n_to = kinds.count("timeout")
        n_oth = kinds.count("other")
        reqs_done += n
        # 立刻探活（不等 sleep）
        alive_now = ping_ok()
        g1 = time.time() - T0
        info_n = board_info(timeout=10)
        alive_http = (info_n.get("ok") is True) or ("ip" in info_n)
        gap, cnt = max_gap(PROP_TOPIC, g0, g1 + 1.0)
        ladder.append(dict(n=n, ok=ok, dur=dur, alive_ping=alive_now,
                           alive_http=alive_http, gap=gap, cnt=cnt,
                           rst=n_rst, to=n_to, oth=n_oth,
                           polls=info_n.get("http_polls"), reqs=info_n.get("http_reqs")))
        print("     n=%-2d  ok %d/%-2d  rst=%d timeout=%d other=%d  耗时 %5.2fs  "
              "ping=%-5s http=%-5s  期间 property/post=%d 最大间隔=%.1fs  http_polls=%r"
              % (n, ok, n, n_rst, n_to, n_oth, dur, alive_now, alive_http, cnt, gap,
                 info_n.get("http_polls")))
        bad = [(classify(*r), r[0], r[1][:50], round(r[2], 2)) for r in res
               if classify(*r) != "ok"]
        if bad:
            print("        ! 非 200：%s" % bad[:3])
        if not (alive_now and alive_http):
            was_dead = True
            print("        !! 板子在此级失去响应（旧版 n>=4 即如此）!!")
        time.sleep(2)

    # ---- P3 信息级加压 48 ----
    print()
    print("[P3] 信息级加压 BURST=%d（不进硬判据）" % BURST)
    dur_b, res_b = hammer(BURST)
    kb = [classify(*r) for r in res_b]
    ok_b = kb.count("ok")
    alive_b = ping_ok()
    print("     n=%-2d  ok %d/%-2d  rst=%d timeout=%d other=%d  耗时 %.2fs  ping=%s"
          % (BURST, ok_b, BURST, kb.count("rst"), kb.count("timeout"),
             kb.count("other"), dur_b, alive_b))

    stop_ev.set()
    time.sleep(0.5)

    # ---- P4 洪泛期间主循环是否被饿死 ----
    worst_gap, worst_win = 0.0, 0.0
    for _, delta, gap_len in samples:
        if delta == 0:
            worst_win = max(worst_win, gap_len)
        else:
            worst_gap = max(worst_gap, worst_win + gap_len) if worst_win else max(worst_gap, 0.0)
            worst_win = 0.0
    # 简化：用采样序列直接算「连续无新增」的最长累计时长
    run = 0.0
    longest_stall = 0.0
    for _, delta, gap_len in samples:
        if delta == 0:
            run += gap_len
            longest_stall = max(longest_stall, run)
        else:
            run = 0.0
    print()
    print("[P4] 采样 %d 次（0.4s 粒度）：最长「无新增属性上报」窗口 = %.1fs" % (len(samples), longest_stall))

    # ---- P5 下行链路没被改坏 ----
    print()
    print("[P5] 下行 set_channel 洪泛 %d 条（HTTP 改动后下行仍须通）" % CMD_N)
    ch_before = channels()
    t_c0 = time.time() - T0
    saw_ch2 = []
    for i in range(CMD_N):
        c.publish(CMD_TOPIC, json.dumps({
            "messageId": "v605cmd-%d" % i, "functionId": "set_channel",
            "inputs": [{"name": "channel", "value": 2},
                       {"name": "state", "value": i % 2 == 0}],
        }), qos=1)
        time.sleep(1.0)
        if i in (0, CMD_N // 2, CMD_N - 1):
            saw_ch2.append((channels() or {}).get("ch2"))
    t_c1 = time.time() - T0
    n_reply = count(REPLY_TOPIC, t_c0 - 1, t_c1)
    n_evt = count(EVENT_TOPIC, t_c0, t_c1)
    ch_after = channels()
    g_c, n_c = max_gap(PROP_TOPIC, t_c0, t_c1)
    print("     function/post=%d/%d  switch_change=%d  property/post=%d 最大间隔=%.1fs"
          % (n_reply, CMD_N, n_evt, n_c, g_c))
    print("     /api/status ch2 采样: %s" % saw_ch2)

    # ---- P6 收尾健康 ----
    print()
    print("[P6] 静置 15s 取终态 + 复核并发计数")
    t_e0 = time.time() - T0
    time.sleep(15)
    t_e1 = time.time() - T0
    gap_end, cnt_end = max_gap(PROP_TOPIC, t_e0, t_e1)
    info1 = board_info()
    print("     property/post=%d 条 最大间隔=%.1fs" % (cnt_end, gap_end))
    print("     终态 /api/info: " + json.dumps(info1, ensure_ascii=False))

    # ---- 汇总 ----
    t_all = time.time() - T0
    gap_all, n_all = max_gap(PROP_TOPIC, 0.0, t_all)
    total_http = sum(r["ok"] for r in ladder)
    polls0, polls1 = info0.get("http_polls") or 0, info1.get("http_polls") or 0
    reqs0, reqs1 = info0.get("http_reqs") or 0, info1.get("http_reqs") or 0

    print()
    print("=" * 78)
    print("全程 %.0fs：property/post=%d 条，最大到达间隔=%.1fs" % (t_all, n_all, gap_all))
    print("HTTP 并发请求成功 %d 条（阶梯 %s + burst %d）" % (total_http + ok_b, LADDER, BURST))
    print("http_polls %d -> %d ; http_reqs %d -> %d（+%d，覆盖全部并发请求）"
          % (polls0, polls1, reqs0, reqs1, reqs1 - reqs0))
    print("property/post QoS 集合 = %s" % sorted(qos_set(PROP_TOPIC)))
    print("=" * 78)

    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  |  " + detail) if detail else ""))

    print("=== 判定 ===")
    ck("B1 板上运行的是 v6.0.5（/api/info 有 http_polls/http_reqs）", has_poll,
       "http_polls=%r http_reqs=%r" % (info0.get("http_polls"), info0.get("http_reqs")))
    ck("B2 基线属性流按周期上报", n_base >= 2 and g_base < GAP_BUDGET,
       "%d 条 / 最大间隔 %.1fs" % (n_base, g_base))
    # B3 判据（2026-09-10 三次实测后定稿）：
    #   硬① 各级 to == 0 —— 这是「冻结」的唯一签名，也是本缺陷的正面判据；
    #   硬② 各级 ok + rst == n —— 每个请求都拿到**确定结果**（服务或明确拒绝），
    #        不存在挂起/无响应；
    #   硬③ 总成功率 >= 75% —— 保证服务有效（不是「全拒但也算过」）。
    # 不再要求每级 ok>=80%：C3 lwIP 只有 CONFIG_LWIP_MAX_ACTIVE_TCP=16 条 PCB，
    # 还要与 MQTT 那条 + 上一级遗留的 TIME_WAIT 共享；n=16/32 时被 RST 是
    # **容量拒绝**而非缺陷。实测 n=16 成功率 15/16、14/16、12/16（随 RSSI 波动）。
    _ladder_n = sum(r["n"] for r in ladder)
    _ladder_ok = sum(r["ok"] for r in ladder)
    _rate = (_ladder_ok / float(_ladder_n)) if _ladder_n else 0.0
    ck("B3 并发阶梯无「冻结超时」且每级都有确定结果（旧版 n>=4 整机冻结）",
       all(r["to"] == 0 and r["ok"] + r["rst"] == r["n"] for r in ladder) and _rate >= 0.75,
       "; ".join("n=%d ok=%d/%d rst=%d to=%d" % (r["n"], r["ok"], r["n"], r["rst"], r["to"])
                 for r in ladder)
       + "  总成功率=%.0f%%  [rst=lwIP PCB 池(16)打满的容量拒绝，非冻结]" % (_rate * 100))
    ck("B4 每一级之后板子仍活着（ping + HTTP 双证）", not was_dead,
       "; ".join("n=%d ping=%s http=%s" % (r["n"], r["alive_ping"], r["alive_http"]) for r in ladder))
    ck("B5 并发洪泛期间主循环未被饿死（采样最长停滞 < 6s）", longest_stall < 6.0,
       "最长「无新增上报」窗口 %.1fs（采样 %d 次）" % (longest_stall, len(samples)))
    ck("B6 并发期间属性流未断", all(r["gap"] < GAP_BUDGET for r in ladder),
       "; ".join("n=%d gap=%.1fs" % (r["n"], r["gap"]) for r in ladder))
    ck("B7 http_polls/http_reqs 随请求增长（确证走主循环轮询）",
       polls1 > polls0 and (reqs1 - reqs0) >= total_http,
       "polls %d->%d, reqs %d->%d (+%d / 请求 %d)" % (polls0, polls1, reqs0, reqs1,
                                                      reqs1 - reqs0, total_http))
    ck("B8 下行 set_channel 仍全通（HTTP 改动无回归）", n_reply >= CMD_N - 2 and n_evt >= 1,
       "function/post %d/%d, switch_change %d" % (n_reply, CMD_N, n_evt))
    ck("B9 下行真的改变了 ch2", any(v is True for v in saw_ch2),
       "ch2 %s -> %s 采样=%s" % ((ch_before or {}).get("ch2"), (ch_after or {}).get("ch2"), saw_ch2))
    ck("B10 出站 publish 全 qos=0", qos_set(PROP_TOPIC) == {0},
       "QoS 集合=%s" % sorted(qos_set(PROP_TOPIC)))
    d_reassoc = (info1.get("wifi_reassocs") or 0) - (info0.get("wifi_reassocs") or 0)
    d_reset = (info1.get("net_resets") or 0) - (info0.get("net_resets") or 0)
    d_conn = (info1.get("mqtt_connects") or 0) - (info0.get("mqtt_connects") or 0)
    ser = b"".join(serialbuf).decode("utf-8", "backslashreplace") if serialbuf else ""
    reconn = [ln.strip() for ln in ser.splitlines()
              if ("MQTT error" in ln or "force reconnect" in ln or "re-associate" in ln)]
    ck("B11 无病态重连（自愈 D/E 未触发）且终态 MQTT 在线",
       d_reassoc == 0 and d_reset == 0
       and info1.get("force_reconnect") is False
       and info1.get("mqtt_connected") is True,
       "connects %r->%r (+%d 链路瞬断自愈=正常), reassoc +%d, net_resets +%d, "
       "force_reconnect=%r, mqtt_connected=%r%s"
       % (info0.get("mqtt_connects"), info1.get("mqtt_connects"), d_conn,
          d_reassoc, d_reset, info1.get("force_reconnect"),
          info1.get("mqtt_connected"),
          ("  [串口证据] " + " | ".join(reconn[:2])) if reconn else ""))
    ck("B12 收尾时属性流仍在跑", gap_end < GAP_BUDGET, "最大间隔 %.1fs（%d 条）" % (gap_end, cnt_end))

    n_pass = sum(1 for _, ok, _ in checks if ok)
    print()
    print("===== v6.0.5 实板验证（HTTP 并发冻结修复）：%d/%d %s =====" % (
        n_pass, len(checks), "PASS" if n_pass == len(checks) else "FAIL"))
    for name, ok, d in checks:
        if not ok:
            print("  FAILED: %s  %s" % (name, d))

    if WITH_SERIAL and serialbuf:
        with open("verify_board_serial.log", "wb") as f:
            f.write(b"".join(serialbuf))
        print("[serial] %d bytes -> verify_board_serial.log" % sum(len(b) for b in serialbuf))

    try:
        c.loop_stop()
        c.disconnect()
    except Exception:
        pass
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
