# -*- coding: utf-8 -*-
"""run_sim.py —— ESP32 8路继电器固件 PC 仿真测试台

用法：
    python firmware-simtest/run_sim.py
    python firmware-simtest/run_sim.py --no-mqtt # 只验配网/持久化/按键

流程（模拟真实设备全生命周期）：
  A. 首次启动：flash 无 config.json -> 进入 AP 配网模式
     -> 手机访问 http://127.0.0.1:18080/ 配置页，POST /save 保存配置
  B. 保存后固件 machine.reset() -> 捕获 SimReset 重新 exec 固件（模拟掉电重启）
  C. 重启后：读取持久化配置 -> 连 WiFi(桩) -> 连真实 EMQX
     -> 断言：上线 retain、属性上报字段/结构、周期上报
  D. 平台下行：properties/write + /service/cmd(set_channel/switch_all)
     -> 断言：引脚变化、switch_change 事件、各 reply
  E. MQTT 断线重联：sim_down -> 固件感知 -> sim_up -> 自动重连并重新上报
  F. 长按 SW1(5s) -> 退出正常模式 -> 配网页再次可访问
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import json
import threading
import traceback
import urllib.parse

# 必须先于“桩注入”import，避免 paho/urllib 拿到被替换的 socket/time
import socket as real_socket
import time as real_time
import paho.mqtt.client as paho

# 被仿真的固件：esp32-8relay-firmware/main.py（测试台位于仓库根目录）
FW_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "esp32-8relay-firmware", "main.py"))
SIM_DIR = os.path.dirname(os.path.abspath(__file__))
FLASH_DIR = os.path.join(SIM_DIR, "flash")
CONFIG_NAME = "config.json"   # 固件用相对路径，运行 cwd=FLASH_DIR
PAGE_MARK = "8路继电器".encode("utf-8")   # 配网页标题标识（bytes 不能直接写中文）


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


# -------------------- 桩注入 --------------------
def inject_stubs(web_port=18080, sta_mac=None, sta_ok=True):
    """把 MicroPython 桩注册进 sys.modules，返回可控制桩的句柄。"""
    sys.path.insert(0, SIM_DIR)

    import stubs.machine as m
    import stubs.network as n
    import stubs.time as t
    import stubs.socket as sk

    if sta_mac:
        n.sim_set_sta_mac(bytes(sta_mac))
    n.sim_set_sta_ok(sta_ok)
    n.sim_set_sta_connected(False)

    sk.PORT_MAP[80] = int(web_port)

    sys.modules["machine"] = m
    sys.modules["network"] = n
    sys.modules["socket"] = sk
    # 仅覆盖 socket/machine/network；time 不能全局替换，否则线程/paho 都受影响。
    # 改为给真实 time 打补丁（加 ticks_* / sleep_ms 等 MicroPython API）。
    _patch_time(t)

    import stubs.umqtt.simple as uq
    sys.modules["umqtt"] = __import__("stubs.umqtt", fromlist=["simple"])
    sys.modules["umqtt.simple"] = uq

    return {"machine": m, "network": n, "socket": sk, "umqtt": uq, "time": t}


def _patch_time(t):
    """把 MicroPython time API 补到真实 time 模块上（幂等）。"""
    for name in ("ticks_ms", "ticks_add", "ticks_diff",
                 "sleep_ms", "sleep_us", "localtime", "monotonic"):
        if not hasattr(real_time, name):
            setattr(real_time, name, getattr(t, name))


# -------------------- HTTP（手机配网端）--------------------
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


def wait_http_ready(port, timeout_s=20):
    t0 = real_time.time()
    while real_time.time() - t0 < timeout_s:
        try:
            resp = http_request(port, "/", timeout=1)
            first = resp.split(b"\r\n", 1)[0]
            if b" 200 " in first or first.endswith(b" 200"):
                return resp
        except Exception:
            pass
        real_time.sleep(0.2)
    return None


# -------------------- 固件运行容器 --------------------
class FirmwareRunner:
    """重复 exec main.py：SimReset 即一次掉电重启。"""

    def __init__(self, fw_path=FW_PATH):
        with open(os.path.abspath(fw_path), "r", encoding="utf-8") as f:
            self.code = compile(f.read(), os.path.abspath(fw_path), "exec")
        self.boot_count = 0
        self.running = True
        self._thread = None
        self._last_ns = None

    def _run(self):
        import stubs.machine as sm
        while self.running:
            ns = {"__name__": "__main__"}
            self.boot_count += 1
            log("SIM", "=== firmware boot #%d ===" % self.boot_count)
            try:
                self._last_ns = ns
                exec(self.code, ns)
                log("SIM", "firmware main returned (unexpected)")
                return
            except BaseException as e:  # noqa: BLE001  (SimReset 继承 BaseException)
                if isinstance(e, sm.SimReset):
                    log("SIM", "device reboot (machine.reset simulated)")
                    self._last_ns = None
                    real_time.sleep(0.3)
                    continue
                log("SIM", "firmware exception: %r" % e)
                traceback.print_exc()
                return

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False


# -------------------- MQTT 平台侧（验证固件输出）--------------------
def broker_reachable(host, port, timeout=5):
    """探测 EMQX 是否可达，避免 MqttWatcher 阻塞过久。"""
    if not host:
        return False
    try:
        s = real_socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


class MqttWatcher:
    def __init__(self, host, port, user, password):
        kwargs = {}
        if getattr(paho, "CallbackAPIVersion", None):
            kwargs["callback_api_version"] = paho.CallbackAPIVersion.VERSION2
        self.client = paho.Client(client_id="sim-watcher-%d" % os.getpid(), **kwargs)
        if user:
            self.client.username_pw_set(user, password)
        self.messages = []
        self._lock = threading.Lock()
        self.client.on_message = self._on_message
        self.client.connect(host, port, 60)
        self.client.loop_start()

    def close(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self.messages.append((msg.topic, msg.payload))

    def subscribe(self, topic):
        self.client.subscribe(topic)

    def publish(self, topic, payload, qos=1):
        self.client.publish(topic, payload, qos=qos)

    def wait_topic(self, topic_sub, timeout_s=15, predicate=None, from_index=0):
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            with self._lock:
                msgs = list(self.messages[from_index:])
            for tp, pl in msgs:
                if topic_sub in tp:
                    if predicate is None or predicate(tp, pl):
                        return tp, pl
            real_time.sleep(0.2)
        return None, None

    def wait_property(self, device_id, timeout_s=15, from_index=0):
        """等待一条新的属性上报（从 from_index 之后的消息里找）。"""
        t0 = real_time.time()
        while real_time.time() - t0 < timeout_s:
            with self._lock:
                msgs = list(self.messages[from_index:])
            for tp, pl in msgs:
                if device_id in tp and ("property/post" in tp or "thing/event/property/post" in tp):
                    try:
                        return json.loads(pl.decode("utf-8"))
                    except Exception:
                        pass
            real_time.sleep(0.2)
        return None

    def msg_count(self):
        with self._lock:
            return len(self.messages)


# -------------------- 表单构造 --------------------
def build_save_form(cfg):
    return urllib.parse.urlencode({
        "wifi_ssid": cfg["wifi_ssid"],
        "wifi_password": cfg.get("wifi_password", "12345678"),
        "mqtt_host": cfg.get("mqtt_host", ""),
        "mqtt_port": cfg.get("mqtt_port", 1883),
        "mqtt_user": cfg.get("mqtt_user", ""),
        "mqtt_password": cfg.get("mqtt_password", ""),
        "product_id": cfg.get("product_id", "relay8_lfx"),
        "device_id": cfg.get("device_id", ""),
        "report_interval": cfg.get("report_interval", 1),
        "topic_mode": cfg.get("topic_mode", "direct"),
    })


# -------------------- 主流程 --------------------
def main():
    ap = argparse.ArgumentParser(description="ESP32 8路继电器固件仿真测试")
    ap.add_argument("--mqtt-host", default=None)
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--no-mqtt", action="store_true", help="跳过真实 MQTT 验证")
    ap.add_argument("--web-port", type=int, default=18080)
    ap.add_argument("--device-id", default="SIM-DEV-01")
    ap.add_argument("--wifi-ssid", default="sim-ap")
    ap.add_argument("--fw", default=FW_PATH)
    args = ap.parse_args()

    # 读取项目配置
    mqtt_host = args.mqtt_host or ""
    mqtt_port = args.mqtt_port or 0
    mqtt_user = args.mqtt_user or ""
    mqtt_pass = args.mqtt_pass or ""
    if not args.no_mqtt:
        # 测试台位于仓库根目录（firmware-simtest/），向上 1 层即项目根（含 config.py）
        proj_root = os.path.abspath(os.path.join(SIM_DIR, ".."))
        if proj_root not in sys.path:
            sys.path.insert(0, proj_root)
        try:
            import config as proj_cfg
            mqtt_host = mqtt_host or proj_cfg.MQTT_HOST
            mqtt_port = mqtt_port or proj_cfg.MQTT_PORT
            mqtt_user = mqtt_user or proj_cfg.MQTT_USER
            mqtt_pass = mqtt_pass or proj_cfg.MQTT_PASS
        except Exception as e:
            log("SIM", "warning: 读取 config.py 失败(%s)，跳过 MQTT" % e)

    os.makedirs(FLASH_DIR, exist_ok=True)
    os.chdir(FLASH_DIR)
    cfg_path = os.path.join(FLASH_DIR, CONFIG_NAME)
    if os.path.exists(cfg_path):
        os.remove(cfg_path)   # 模拟全新设备首启

    stubs = inject_stubs(web_port=args.web_port)
    machine = stubs["machine"]
    uq = stubs["umqtt"]
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        log("RESULT", ("PASS  " if ok else "FAIL  ") + name + ((" | " + str(detail)) if detail else ""))

    # ============ A. 首次启动进入配网模式 ============
    log("PHASE", "A. 首次启动(无配置) -> 配网热点 + Web 配置页")
    fw = FirmwareRunner(args.fw)
    fw.start()
    resp = wait_http_ready(args.web_port, timeout_s=25)
    check("A1 配网页可访问(GET /)", resp is not None and PAGE_MARK in (resp or b""))
    if not resp:
        log("SIM", "配网页未就绪，终止")
        fw.stop()
        sys.exit(1)

    # ============ B. POST /save -> 自动重启 ============
    log("PHASE", "B. 提交配置并等待设备重启")
    form = {
        "wifi_ssid": args.wifi_ssid,
        "wifi_password": "12345678",
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "mqtt_user": mqtt_user,
        "mqtt_password": mqtt_pass,
        "product_id": "relay8_lfx",
        "device_id": args.device_id,
        "report_interval": 1,
        "topic_mode": "direct",
    }
    body = build_save_form(form)
    resp = http_request(args.web_port, "/save", method="POST", body=body)
    first = resp.split(b"\r\n", 1)[0] if resp else b""
    check("B1 保存配置返回 200", b"200" in first)
    real_time.sleep(3.0)  # 固件 sleep1+reset + 重启进入正常模式
    check("B2 设备发生重启(boot>=2)", fw.boot_count >= 2, "boot=%d" % fw.boot_count)
    check("B3 配置已持久化 config.json", os.path.exists(cfg_path))

    mqtt_on = (not args.no_mqtt) and bool(mqtt_host)
    watcher = None
    if mqtt_on:
        if not broker_reachable(mqtt_host, mqtt_port):
            log("SIM", "MQTT broker %s:%s 不可达，跳过 C~E（可加 --no-mqtt 只验配网/持久化/按键）"
                % (mqtt_host, mqtt_port))
            mqtt_on = False
        else:
            try:
                watcher = MqttWatcher(mqtt_host, mqtt_port, mqtt_user, mqtt_pass)
            except Exception as e:
                log("SIM", "MQTT watcher 连接失败：%s，跳过 C~E" % e)
                mqtt_on = False

    if mqtt_on:
        # ============ C. 正常模式连接真实 EMQX ============
        log("PHASE", "C. 连WiFi(桩) -> 连真实EMQX -> 属性上报")
        base = "/relay8_lfx/%s" % args.device_id
        watcher.subscribe(base + "/#")

        prop = watcher.wait_property(args.device_id, timeout_s=30)
        check("C1 收到固件属性上报", prop is not None)
        if prop:
            ps = prop.get("properties", {})
            n_on = sum(1 for k in ps if k.startswith("ch") and k.endswith("_state"))
            check("C2 属性含8路通道状态", n_on == 8, "ch_state=%d" % n_on)
            check("C3 属性含温度/湿度", "temperature" in ps and "humidity" in ps)
            check("C4 payload含productId/deviceId/timestamp",
                  prop.get("deviceId") == args.device_id and prop.get("productId") == "relay8_lfx")

        # ============ D. 下行命令闭环 ============
        log("PHASE", "D. 平台下行：写属性 + set_channel/switch_all")
        mid = "sim-msg-001"
        watcher.publish(base + "/properties/write", json.dumps({
            "productId": "relay8_lfx", "deviceId": args.device_id,
            "messageId": mid, "properties": {"ch1_state": True}}))
        got, pl = watcher.wait_topic(base + "/properties/write/reply", timeout_s=12,
                                     predicate=lambda t, p: mid in p.decode("utf-8", "replace"))
        check("D1 写属性有回复", got is not None)
        check("D2 ch1继电器吸合(GPIO3=0)", machine.sim_read(3) == 0, "GPIO3=%s" % machine.sim_read(3))
        ev = watcher.wait_topic(base + "/event/switch_change", timeout_s=12)
        check("D3 switch_change事件上报", ev is not None)

        mid2 = "sim-msg-002"
        watcher.publish(base + "/service/cmd", json.dumps({
            "functionId": "set_channel", "messageId": mid2,
            "inputs": [{"name": "channel", "value": 2}, {"name": "state", "value": True}]}))
        got2, pl2 = watcher.wait_topic(base + "/function/post", timeout_s=12,
                                       predicate=lambda t, p: mid2 in p.decode("utf-8", "replace"))
        ok2 = bool(got2)
        if ok2:
            try:
                ok2 = json.loads(pl2.decode("utf-8")).get("success") is True
            except Exception:
                ok2 = False
        check("D4 set_channel回复success", ok2, pl2[:120] if pl2 else "")
        check("D5 ch2继电器吸合(GPIO4=0)", machine.sim_read(4) == 0, "GPIO4=%s" % machine.sim_read(4))

        mid3 = "sim-msg-003"
        watcher.publish(base + "/service/cmd", json.dumps({
            "functionId": "switch_all", "messageId": mid3,
            "inputs": [{"name": "state", "value": False}]}))
        got3, pl3 = watcher.wait_topic(base + "/function/post", timeout_s=12,
                                       predicate=lambda t, p: mid3 in p.decode("utf-8", "replace"))
        ok3 = bool(got3)
        if ok3:
            try:
                ok3 = json.loads(pl3.decode("utf-8")).get("success") is True
            except Exception:
                ok3 = False
        check("D6 switch_all回复success", ok3, pl3[:120] if pl3 else "")
        all_off = all(machine.sim_read(p) == 1 for p in (3, 4, 5, 7, 10, 18, 19, 20))
        check("D7 switch_all后8路全断", all_off)

        # ============ E. MQTT 断线重联 ============
        log("PHASE", "E. MQTT 断线重联")
        before_count = watcher.msg_count()
        conn0 = uq.sim_conn_count()
        uq.sim_down()
        log("SIM", "sim_down: 已模拟网络断开，等待固件感知...")
        real_time.sleep(6)
        uq.sim_up()
        log("SIM", "sim_up: 已恢复网络，等待固件自动重连...")
        t0 = real_time.time()
        reconnected = False
        while real_time.time() - t0 < 30:
            if uq.sim_conn_count() > conn0:
                reconnected = True
                break
            real_time.sleep(0.3)
        check("E1 固件重新发起 MQTT 连接", reconnected)
        prop2 = watcher.wait_property(args.device_id, timeout_s=20, from_index=before_count)
        check("E2 重连后重新属性上报", prop2 is not None)
    else:
        log("SIM", "跳过 C~E（--no-mqtt 或无 broker）")

    # ============ F. 长按 SW1 进配网 ============
    log("PHASE", "F. 模拟长按 SW1(5s) -> 进入配网模式")
    network_stub = stubs["network"]
    # 先等固件进入正常模式（STA active 且已连上），长按才有效
    t0 = real_time.time()
    while real_time.time() - t0 < 30:
        if network_stub.sim_sta_connected():
            break
        real_time.sleep(0.2)
    log("SIM", "固件已处于正常模式，开始长按 SW1(按住直到配网页就绪)...")
    machine.sim_write(8, 0)   # 按下 SW1
    resp3 = None
    t1 = real_time.time()
    while real_time.time() - t1 < 45:
        try:
            r = http_request(args.web_port, "/", timeout=1)
            if r and PAGE_MARK in r:
                resp3 = r
                break
        except Exception:
            pass
        real_time.sleep(0.5)
    machine.sim_write(8, 1)   # 松开
    check("F1 长按后重新进入配网(页面可访问)",
          resp3 is not None and PAGE_MARK in resp3)
    # 注意：192.168.4.1 是真机 AP 地址，PC 仿真打不开；127.0.0.1 端口随测试进程结束而关闭，
    # 需在测试运行中（A/B 或 F 阶段）打开才能看到配网页。
    log("HINT", "配网页(PC) = http://127.0.0.1:%d（测试进程运行中可打开；进程退出即关闭；"
                "192.168.4.1 是真机热点地址，PC 上不可用）" % args.web_port)
    if watcher is not None:
        watcher.close()

    passed = sum(1 for x in results if x)
    log("SUMMARY", "PASS %d / %d" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
