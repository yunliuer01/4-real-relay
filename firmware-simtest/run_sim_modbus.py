# -*- coding: utf-8 -*-
"""run_sim_modbus.py —— 4路继电器 + Modbus RTU 采集网关固件 PC 仿真测试台（无硬件）

对被测固件：esp32-relay4-modbus-gateway/main.py + modbus_master.py

与旧 run_sim.py 的差异：
- 目标固件升级为 4 路继电器 + Modbus 网关（relay4_lfx / IO3,4,5,7 / SW1=IO10）
- machine 桩新增 UART + 内存仿真 Modbus 从站总线（多从站多寄存器、可注入
  掉线/异常/CRC 错误/寄存器数值变化）
- umqtt 桩新增“环回录播”模式：没有真实 broker 时也在本地完成连接/收发，
  因此本测试台在任意 PC（无需内网/无 broker）都能跑完整链路
- 核心验证目标：
  C. 采集值(带 sN_ 前缀与无前缀)合并进属性上报 + [MODBUS] 关键日志
  D. 从站“假死/超时”期间继电器 MQTT 命令仍秒回（采集不阻塞控制）
  E. 从站故障：错误日志、旧值保留、恢复后新值生效
  G. MQTT 断线期间 Modbus 采集线程继续工作
  F. 长按 SW1 回配网时 Modbus 线程优雅停止

用法：
    python firmware-simtest/run_sim_modbus.py                 # 自动选择后端
    python firmware-simtest/run_sim_modbus.py --backend loopback
    python firmware-simtest/run_sim_modbus.py --backend real   # 强制连真实 EMQX
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import json
import threading

# 必须在桩注入/固件 exec 前 import 真实依赖
import socket as real_socket
import time as real_time

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
FW_DIR = os.path.abspath(os.path.join(PROJ_ROOT, "esp32-relay4-modbus-gateway"))
FLASH_DIR = os.path.join(SIM_DIR, "flash_modbus")
CONFIG_NAME = "config.json"

# 配网页标题标识（bytes 不能直接写中文）
PAGE_MARK = "4路继电器".encode("utf-8")

PRODUCT_ID = "relay4_lfx"
DEVICE_ID = "SIM-MB-01"

# 仿真从站初始寄存器表（与测试配置的 slaves 对应）
SLAVE1 = 1
SLAVE2 = 2
INIT_REGS = {SLAVE1: {0: 250, 1: 456}, SLAVE2: {5: 1234}}

# 通过配网页提交的 Modbus JSON（可加/减从站与寄存器，验证多从站多周期）
MODBUS_JSON = {
    "enabled": True,
    "uart_id": 1,
    "baudrate": 9600,
    "tx_pin": 20,
    "rx_pin": 21,
    "dir_pin": 8,
    "timeout_ms": 300,
    "retries": 2,
    "retry_interval_ms": 100,
    "slaves": [
        {"slave_id": SLAVE1, "registers": [
            {"addr": 0, "func": 3, "key": "temperature", "scale": 0.1,
             "period_ms": 1000, "signed": False, "digits": 2},
            {"addr": 1, "func": 3, "key": "humidity", "scale": 0.1,
             "period_ms": 1000, "signed": False, "digits": 2},
        ]},
        {"slave_id": SLAVE2, "registers": [
            {"addr": 5, "func": 3, "key": "mb_value", "scale": 1.0,
             "period_ms": 1500, "signed": False, "digits": 0},
        ]},
    ],
}


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


# -------------------- 日志捕获（固件 print 全量入缓冲区） --------------------
class LogBuffer:
    """替换 sys.stdout：tee 到真实 stdout + 追加记录，供断言与等待。"""

    def __init__(self, real):
        self._real = real
        self._lock = threading.Lock()
        self._lines = []

    def write(self, s):
        # 加锁保证多线程(固件主循环/Modbus线程/测试主线程)并发 print 不粘行
        with self._lock:
            self._lines.append(s)
            try:
                self._real.write(s)
                self._real.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def text(self):
        with self._lock:
            return "".join(self._lines)

    def pos(self):
        """当前记录长度游标（用于计数增量）。"""
        with self._lock:
            return len(self._lines)

    def count_from(self, pos, sub):
        with self._lock:
            seg = "".join(self._lines[pos:])
        return seg.count(sub)

    def contains(self, sub):
        return sub in self.text()


def wait_log(lb, substr, timeout_s=15):
    """等待固件/Modbus 线程打印出指定子串。"""
    t0 = real_time.time()
    while real_time.time() - t0 < timeout_s:
        if lb.contains(substr):
            return True
        real_time.sleep(0.2)
    return False


# -------------------- 桩注入 --------------------
def _patch_time(stubs_time):
    for name in ("ticks_ms", "ticks_add", "ticks_diff",
                 "sleep_ms", "sleep_us", "localtime", "monotonic"):
        if not hasattr(real_time, name):
            setattr(real_time, name, getattr(stubs_time, name))


def inject_stubs(web_port=18081):
    sys.path.insert(0, SIM_DIR)
    sys.path.insert(0, FW_DIR)  # 让固件能 import modbus_master
    sys.path.insert(0, PROJ_ROOT)

    import stubs.machine as m
    import stubs.network as n
    import stubs.time as t
    import stubs.socket as sk
    import stubs.umqtt.simple as uq

    n.sim_set_sta_ok(True)
    n.sim_set_sta_connected(False)
    n.sim_set_sta_mac(bytes([0x24, 0x0a, 0xc4, 0x00, 0x12, 0x34]))
    sk.PORT_MAP[80] = int(web_port)
    # loopback 模式下固件的 mqtt_host/mqtt_port 是占位地址（真实 MQTT 走进程内
    # 总线：run_sim_modbus 用 (127.0.0.1, 1)，独立测试脚本用 (127.0.0.1, 1883)）。
    # v6.0.5 固件在建连前会做带超时的 TCP 预检，必须让这些占位端点「语义上可达」，
    # 否则固件连不进总线（表现为 A1/A2 全超时）。
    for _ep in (("127.0.0.1", 1), ("127.0.0.1", 1883)):
        sk.sim_add_virtual_endpoint(*_ep)

    sys.modules["machine"] = m
    sys.modules["network"] = n
    sys.modules["socket"] = sk
    _patch_time(t)
    sys.modules["umqtt"] = __import__("stubs.umqtt", fromlist=["simple"])
    sys.modules["umqtt.simple"] = uq
    return {"machine": m, "network": n, "socket": sk, "umqtt": uq, "time": t}


# -------------------- HTTP（配网端） --------------------
def http_request(port, path="/", method="GET", body=None, timeout=5):
    s = real_socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        if method == "GET":
            req = "GET %s HTTP/1.0\r\nHost: 192.168.4.1\r\nConnection: close\r\n\r\n" % path
            s.sendall(req.encode("utf-8"))
        else:
            req = ("POST %s HTTP/1.0\r\nHost: 192.168.4.1\r\n"
                   "Content-Type: application/x-www-form-urlencoded\r\n"
                   "Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                   % (path, len(body), body))
            s.sendall(req.encode("utf-8"))
        chunks = []
        while True:
            d = s.recv(4096)
            if not d:
                break
            chunks.append(d)
        return b"".join(chunks)
    finally:
        s.close()


def wait_http_ready(port, timeout_s=25, mark=PAGE_MARK):
    t0 = real_time.time()
    while real_time.time() - t0 < timeout_s:
        try:
            resp = http_request(port, "/", timeout=1)
            if resp and mark in resp:
                return resp
        except Exception:
            pass
        real_time.sleep(0.2)
    return None


# -------------------- 固件运行容器 --------------------
class FirmwareRunner:
    def __init__(self, fw_path):
        with open(os.path.abspath(fw_path), "r", encoding="utf-8") as f:
            self.code = compile(f.read(), os.path.abspath(fw_path), "exec")
        self.boot_count = 0
        self.running = True
        self._thread = None

    def _run(self):
        import stubs.machine as sm
        while self.running:
            ns = {"__name__": "__main__"}
            self.boot_count += 1
            log("SIM", "=== firmware boot #%d ===" % self.boot_count)
            try:
                exec(self.code, ns)
                log("SIM", "firmware main returned (unexpected)")
                return
            except BaseException as e:  # SimReset 继承 BaseException
                if isinstance(e, sm.SimReset):
                    log("SIM", "device reboot (machine.reset simulated)")
                    real_time.sleep(0.3)
                    continue
                log("SIM", "firmware exception: %r" % e)
                import traceback
                traceback.print_exc()
                return

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False


# -------------------- MQTT 后端（real / loopback 统一接口） --------------------
class RealBackend:
    """真实 EMQX：paho 观察端 + 下行注入。"""

    def __init__(self, host, port, user, password):
        import paho.mqtt.client as paho
        kwargs = {}
        if getattr(paho, "CallbackAPIVersion", None):
            kwargs["callback_api_version"] = paho.CallbackAPIVersion.VERSION2
        self.client = paho.Client(client_id="sim-mb-watcher-%d" % os.getpid(), **kwargs)
        if user:
            self.client.username_pw_set(user, password)
        self.messages = []
        self._lock = threading.Lock()
        self.client.on_message = self._on_message
        self.client.connect(host, port, 60)
        self.client.loop_start()
        self.base = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)
        self.client.subscribe(self.base + "/#")

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self.messages.append((msg.topic, msg.payload))

    def wait_property(self, timeout_s=15, from_index=0):
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            with self._lock:
                msgs = list(self.messages[from_index:])
            for tp, pl in msgs:
                if "property/post" in tp:
                    try:
                        return json.loads(pl.decode("utf-8"))
                    except Exception:
                        pass
            real_time.sleep(0.2)
        return None

    def wait_topic(self, topic_sub, timeout_s=15, predicate=None, from_index=0):
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            with self._lock:
                msgs = list(self.messages[from_index:])
            for tp, pl in msgs:
                if topic_sub in tp:
                    pl = pl.decode("utf-8", "replace") if isinstance(pl, (bytes, bytearray)) else pl
                    if predicate is None or predicate(tp, pl):
                        return tp, pl
            real_time.sleep(0.2)
        return None, None

    def downlink(self, topic, payload):
        self.client.publish(topic, payload, qos=1)

    def msg_count(self):
        with self._lock:
            return len(self.messages)

    def close(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


class LoopBackBackend:
    """无 broker：固件侧 umqtt 桩本地“在线”，上行记录 + 下行注入。"""

    def __init__(self, uq):
        self.uq = uq
        uq.sim_set_loopback(True)
        self.base = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)

    def wait_property(self, timeout_s=15, from_index=0):
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            for tp, pl in self.uq.sim_recv_msgs(from_index):
                if "property/post" in tp:
                    try:
                        return json.loads(pl)
                    except Exception:
                        pass
            real_time.sleep(0.2)
        return None

    def wait_topic(self, topic_sub, timeout_s=15, predicate=None, from_index=0):
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            for tp, pl in self.uq.sim_recv_msgs(from_index):
                if topic_sub in tp:
                    if predicate is None or predicate(tp, pl):
                        return tp, pl
            real_time.sleep(0.2)
        return None, None

    def downlink(self, topic, payload):
        if not self.uq.sim_downlink(topic, payload):
            log("SIM", "downlink 未投递(topic=%s)，可能固件未订阅/未连接" % topic)

    def msg_count(self):
        return self.uq.sim_msg_count()

    def close(self):
        self.uq.sim_set_loopback(False)


# -------------------- 表单构造 --------------------
def build_save_form(cfg, modbus_json):
    import urllib.parse
    form = {
        "wifi_ssid": cfg["wifi_ssid"],
        "wifi_password": "12345678",
        "mqtt_host": cfg["mqtt_host"],
        "mqtt_port": cfg["mqtt_port"],
        "mqtt_user": cfg["mqtt_user"],
        "mqtt_password": cfg["mqtt_password"],
        "product_id": PRODUCT_ID,
        "device_id": cfg["device_id"],
        "report_interval": 1,
        "topic_mode": "direct",
        "modbus_json": json.dumps(modbus_json, ensure_ascii=False),
    }
    return urllib.parse.urlencode(form)


# -------------------- 主流程 --------------------
def main():
    ap = argparse.ArgumentParser(description="4路继电器+Modbus网关固件 PC 仿真测试")
    ap.add_argument("--backend", choices=["auto", "real", "loopback"], default="auto")
    ap.add_argument("--mqtt-host", default=None)
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--web-port", type=int, default=18081)
    ap.add_argument("--fw", default=os.path.join(FW_DIR, "main.py"))
    args = ap.parse_args()

    # 日志捕获必须在固件 exec 之前
    lb = LogBuffer(sys.stdout)
    sys.stdout = lb

    # 读取项目真实 MQTT 配置（探测用）
    mqtt_host = args.mqtt_host or ""
    mqtt_port = args.mqtt_port or 0
    mqtt_user = args.mqtt_user or ""
    mqtt_pass = args.mqtt_pass or ""
    try:
        if PROJ_ROOT not in sys.path:
            sys.path.insert(0, PROJ_ROOT)
        import config as proj_cfg
        mqtt_host = mqtt_host or proj_cfg.MQTT_HOST
        mqtt_port = mqtt_port or proj_cfg.MQTT_PORT
        mqtt_user = mqtt_user or proj_cfg.MQTT_USER
        mqtt_pass = mqtt_pass or proj_cfg.MQTT_PASS
    except Exception as e:
        log("SIM", "warning: 读取 config.py 失败(%s)" % e)

    # 后端选择
    backend_mode = args.backend
    broker_ok = bool(mqtt_host) and broker_reachable(mqtt_host, mqtt_port)
    if backend_mode == "auto":
        backend_mode = "real" if broker_ok else "loopback"
    log("SIM", "backend=%s broker=%s:%s reachable=%s" % (backend_mode, mqtt_host, mqtt_port, broker_ok))

    os.makedirs(FLASH_DIR, exist_ok=True)
    os.chdir(FLASH_DIR)
    cfg_path = os.path.join(FLASH_DIR, CONFIG_NAME)
    if os.path.exists(cfg_path):
        os.remove(cfg_path)  # 全新设备首启

    stubs = inject_stubs(web_port=args.web_port)
    machine = stubs["machine"]
    uq = stubs["umqtt"]

    # 注册仿真 Modbus 从站（UART 桩在固件线程中使用同一注册表）
    machine.sim_modbus_reset()
    for sid, regs in INIT_REGS.items():
        machine.sim_modbus_add_slave(sid, regs)

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        log("RESULT", ("PASS  " if ok else "FAIL  ") + name + ((" | " + str(detail)) if detail else ""))

    base = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)

    # ============ 后端初始化 ============
    backend = None
    if backend_mode == "real" and broker_ok:
        try:
            backend = RealBackend(mqtt_host, mqtt_port, mqtt_user, mqtt_pass)
        except Exception as e:
            log("SIM", "real backend 连接失败(%s)，退回 loopback" % e)
            backend = None
    if backend is None:
        backend = LoopBackBackend(uq)
        log("SIM", "使用 loopback 后端（无真实 broker 依赖）")

    fw_path = os.path.abspath(args.fw)
    log("SIM", "firmware: %s" % fw_path)
    fw = FirmwareRunner(fw_path)

    try:
        # ============ A. 首次启动 -> 配网页（含 Modbus JSON 框） ============
        log("PHASE", "A. 首次启动(无配置) -> 配网热点 + Web 配置页")
        fw.start()
        resp = wait_http_ready(args.web_port, timeout_s=25)
        check("A1 配网页可访问且为4路页面",
              resp is not None and PAGE_MARK in (resp or b""))
        check("A2 页面含 Modbus JSON 配置区",
              resp is not None and b"modbus_json" in resp and b"slaves" in resp)

        # ============ B. 提交配置(带 modbus_json) -> 重启 ============
        log("PHASE", "B. 提交配置并等待设备重启")
        # 注意：真实 broker 模式下固件必须用与 watcher 相同的接入账号(mqtt_user=test)，
        # 不能用 DEVICE_ID 当 MQTT 用户名——EMQX 会以 CONNACK rc!=0 拒绝，而固件桩
        # 连接“成功”的假象会让所有 broker 消息级断言全部超时失败(曾致 real 仅 14/24)。
        save_cfg = {
            "wifi_ssid": "sim-ap",
            "mqtt_host": mqtt_host if backend_mode == "real" and broker_ok else "127.0.0.1",
            "mqtt_port": mqtt_port if backend_mode == "real" and broker_ok else 1,
            "mqtt_user": mqtt_user if backend_mode == "real" and broker_ok else DEVICE_ID,
            "mqtt_password": mqtt_pass or "",
            "device_id": DEVICE_ID,
        }
        body = build_save_form(save_cfg, MODBUS_JSON)
        resp = http_request(args.web_port, "/save", method="POST", body=body)
        first = resp.split(b"\r\n", 1)[0] if resp else b""
        check("B1 保存配置返回 200", b"200" in first)
        real_time.sleep(3.5)  # 固件 sleep1 + reset + boot2 连 WiFi(桩,2s)
        check("B2 设备发生重启(boot>=2)", fw.boot_count >= 2, "boot=%d" % fw.boot_count)
        cfg_ok = False
        detail = ""
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                cfg_ok = isinstance(saved.get("modbus"), dict) and \
                    len(saved["modbus"].get("slaves", [])) == 2 and \
                    saved["modbus"]["slaves"][1]["slave_id"] == SLAVE2
                detail = "slaves=%d" % len(saved["modbus"].get("slaves", []))
            except Exception as e:
                detail = "parse err %s" % e
        check("B3 config.json 持久化 Modbus 配置", cfg_ok, detail)

        # ============ C. 正常模式：Modbus 采集 -> 属性合并上报 ============
        log("PHASE", "C. 正常模式 -> Modbus 采集合并进属性上报")
        wait_log(lb, "start_modbus result: True", timeout_s=20)
        prop = backend.wait_property(timeout_s=30)
        check("C1 收到首条属性上报", prop is not None)
        if prop:
            ps = prop.get("properties", {})
            n_state = sum(1 for k in ps if k.startswith("ch") and k.endswith("_state"))
            check("C2 属性含4路通道状态", n_state == 4, "ch_state=%d" % n_state)
            check("C3 payload 含 productId/deviceId",
                  prop.get("deviceId") == DEVICE_ID and prop.get("productId") == PRODUCT_ID)
        else:
            check("C2 属性含4路通道状态", False)
            check("C3 payload 含 productId/deviceId", False)

        # 等一条包含 Modbus 采集值的属性（首报可能早于首个采集周期）
        idx0 = backend.msg_count()
        mb_prop = None
        t0 = real_time.time()
        while real_time.time() - t0 < 25:
            p = backend.wait_property(timeout_s=5, from_index=idx0)
            if p is None:
                break
            ps = p.get("properties", {})
            if "s1_temperature" in ps and "s2_mb_value" in ps:
                mb_prop = p
                break
            idx0 = backend.msg_count()
        ok = mb_prop is not None
        detail = ""
        if mb_prop:
            ps = mb_prop["properties"]
            t_val, h_val, m_val = ps.get("s1_temperature"), ps.get("s1_humidity"), ps.get("s2_mb_value")
            detail = "s1_t=%s s1_h=%s s2_mb=%s" % (t_val, h_val, m_val)
            ok = (t_val == 25.0 and h_val == 45.6 and m_val == 1234
                  and ps.get("temperature") == 25.0 and ps.get("mb_value") == 1234)
        check("C4 属性含采集值且换算正确(s1_/裸key)", ok, detail)

        log_ok = (lb.contains("slave=%d addr=0x0000 key=temperature raw=250 value=25.0" % SLAVE1)
                  and lb.contains("slave=%d addr=0x0005 key=mb_value raw=1234 value=1234" % SLAVE2))
        check("C5 [MODBUS]关键日志(从站/地址/原始值/换算值)", log_ok)

        # 寄存器变化 -> 下一次上报反映新值
        machine.sim_modbus_set(SLAVE1, 0, 321)
        idx1 = backend.msg_count()
        got_new = False
        t0 = real_time.time()
        while real_time.time() - t0 < 15:
            p = backend.wait_property(timeout_s=5, from_index=idx1)
            if p is None:
                break
            if p.get("properties", {}).get("s1_temperature") == 32.1:
                got_new = True
                break
            idx1 = backend.msg_count()
        check("C6 寄存器变化在后续上报生效(raw321->32.1)", got_new)

        # ============ D. 从站假死：不阻塞继电器（核心） ============
        log("PHASE", "D. 从站假死(超时重试)期间，继电器命令仍快速响应")
        machine.sim_modbus_down(SLAVE1, True)
        wait_log(lb, "slave=%d" % SLAVE1, timeout_s=10)
        ok_timeout_log = wait_log(lb, "attempt=", timeout_s=12)
        check("D1 从站假死后出现超时重试日志(attempt)", ok_timeout_log)

        mid = "sim-mb-cmd-001"
        t_start = real_time.time()
        backend.downlink(base + "/service/cmd", json.dumps({
            "functionId": "set_channel", "messageId": mid,
            "inputs": [{"name": "channel", "value": 1}, {"name": "state", "value": True}]}))
        got, pl = backend.wait_topic(base + "/function/post", timeout_s=12,
                                     predicate=lambda t, p: mid in p)
        elapsed = real_time.time() - t_start
        ok_reply = bool(got)
        if ok_reply:
            try:
                ok_reply = json.loads(pl).get("success") is True
            except Exception:
                ok_reply = False
        check("D2 死从站期间命令仍秒回(%.2fs<3s)" % elapsed,
              ok_reply and elapsed < 3.0, "elapsed=%.2fs" % elapsed)
        check("D3 ch1 继电器吸合(GPIO3=0)", machine.sim_read(3) == 0,
              "GPIO3=%s" % machine.sim_read(3))
        ev = backend.wait_topic(base + "/event/switch_change", timeout_s=8)
        check("D4 switch_change 事件上报", ev is not None)

        machine.sim_modbus_down(SLAVE1, False)
        ok_recover = wait_log(lb, "slave=%d addr=0x0000 key=temperature raw=321 value=32.1" % SLAVE1,
                              timeout_s=12)
        check("D5 从站恢复后采集继续(日志再现)", ok_recover)

        # ============ E. 从站故障与恢复：旧值保留 + 新值生效 ============
        log("PHASE", "E. 从站2掉线 -> 错误日志/旧值保留；恢复 -> 新值生效")
        machine.sim_modbus_down(SLAVE2, True)
        err_ok = wait_log(lb, "slave=%d addr=0x0005 key=mb_value err=" % SLAVE2, timeout_s=12)
        check("E1 从站2掉线出现错误日志", err_ok)
        real_time.sleep(3.5)  # 跨过几个采集/上报周期
        idx2 = backend.msg_count()
        keep_prop = backend.wait_property(timeout_s=8, from_index=idx2)
        keep_ok = keep_prop is not None and keep_prop.get("properties", {}).get("mb_value") == 1234
        check("E2 故障期间上报保留最近一次采集值(不丢)", keep_ok)

        machine.sim_modbus_set(SLAVE2, 5, 5678)
        machine.sim_modbus_down(SLAVE2, False)
        idx3 = backend.msg_count()
        got_new2 = False
        t0 = real_time.time()
        while real_time.time() - t0 < 15:
            p = backend.wait_property(timeout_s=5, from_index=idx3)
            if p is None:
                break
            if p.get("properties", {}).get("s2_mb_value") == 5678:
                got_new2 = True
                break
            idx3 = backend.msg_count()
        check("E3 恢复后新采集值生效(5678)", got_new2)

        # ============ G. MQTT 断线期间 Modbus 线程继续采集 ============
        log("PHASE", "G. MQTT 断线期间 Modbus 采集线程不受影响")
        pos = lb.pos()
        conn0 = uq.sim_conn_count()
        uq.sim_down()
        real_time.sleep(4.0)
        inc = lb.count_from(pos, "[MODBUS][INFO]")
        check("G1 断线期间 Modbus 采集日志持续输出(+%d条)" % inc, inc >= 2, "inc=%d" % inc)
        uq.sim_up()
        t0 = real_time.time()
        reconnected = False
        while real_time.time() - t0 < 25:
            if uq.sim_conn_count() > conn0:
                reconnected = True
                break
            real_time.sleep(0.3)
        check("G2 MQTT 自动重连", reconnected)
        idx4 = backend.msg_count()
        prop3 = backend.wait_property(timeout_s=15, from_index=idx4)
        check("G3 重连后属性上报恢复", prop3 is not None)

        # ============ F. 长按 SW1(IO10) 回配网，Modbus 线程停止 ============
        log("PHASE", "F. 长按 SW1(IO10) -> 进入配网模式 + Modbus 线程停止")
        machine.sim_write(10, 0)   # 按下 SW1
        resp3 = wait_http_ready(args.web_port, timeout_s=40)
        machine.sim_write(10, 1)   # 松开
        check("F1 长按后重新进入配网(页面可访问)",
              resp3 is not None and PAGE_MARK in (resp3 or b""))
        check("F2 Modbus 采集线程优雅停止(worker stopped)",
              lb.contains("Modbus worker thread stopped"))

    finally:
        fw.stop()
        backend.close()
        sys.stdout = getattr(sys.stdout, "_real", sys.stdout)

    passed = sum(1 for x in results if x)
    log("SUMMARY", "PASS %d / %d" % (passed, len(results)))
    log("HINT", "配网页(PC) = http://127.0.0.1:%d（仅测试运行中可打开）" % args.web_port)
    sys.exit(0 if passed == len(results) else 1)


def broker_reachable(host, port, timeout=3):
    if not host or not port:
        return False
    try:
        real_socket.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
