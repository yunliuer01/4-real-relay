# -*- coding: utf-8 -*-
"""run_sim_modbus_tcp.py —— 4路继电器 + Modbus TCP 采集网关固件 PC 仿真验收测试（无硬件）

对被测固件：esp32-relay4-modbus-gateway/main.py + modbus_tcp_master.py（mode="tcp"）

与 run_sim_modbus.py(RTU) 的差异：
- 不再用 UART 内存总线仿真，而是起 2 个【真实 Modbus TCP 从站服务器】
  （socket 桩透传真实网络），固件经真实 TCP 连接采集，协议栈完整走真。
- 场景对应用户 Modbus 网关 4 条需求：
  需求1 可配置从站(host/port/unit_id) + 地址(0x0000) + 对应 JSON key(temperature)
  需求2 同一从站多个地址、每个地址独立采集周期（300ms vs 1500ms，用服务器侧请求计数验证）
  需求3 多个从站独立配置(端口/unit/寄存器/周期)，且故障相互隔离（B 挂 A 不受影响）
  需求4 采集(含从站假死/重试)不阻塞继电器：MQTT set_channel 秒回 + GPIO 吸合 + 事件/属性上报

用法：
    python firmware-simtest/run_sim_modbus_tcp.py
    python firmware-simtest/run_sim_modbus_tcp.py --backend loopback   # 无 broker
    python firmware-simtest/run_sim_modbus_tcp.py --backend real       # 真实 EMQX
"""
from __future__ import annotations

import argparse
import os
import sys
import json
import time as real_time
import struct
import threading

import socket as real_socket

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
FW_DIR = os.path.abspath(os.path.join(PROJ_ROOT, "esp32-relay4-modbus-gateway"))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, FW_DIR)
sys.path.insert(0, PROJ_ROOT)

# 复用 RTU 仿真台骨架（stubs/FirmwareRunner/LogBuffer/Backend 等）
import run_sim_modbus as R

FLASH_DIR = os.path.join(SIM_DIR, "flash_modbus_tcp")
CONFIG_NAME = "config.json"
PRODUCT_ID = R.PRODUCT_ID
DEVICE_ID = R.DEVICE_ID            # "SIM-MB-01"，与 backend base 保持一致

# 需求1/2/3 目标配置：两个独立 TCP 从站
S1_PORT, S2_PORT = 15511, 15512
S1_REGS = {0: 250, 1: 456}         # 0x0000 -> temperature(raw250->25.0) 0x0001 -> humidity(45.6)
S2_REGS = {5: 1234, 6: 2200}       # 0x0005 -> energy(1234)            0x0006 -> voltage(220.0)

MODBUS_JSON = {
    "enabled": True,
    "mode": "tcp",
    "timeout_ms": 400,
    "retries": 2,
    "retry_interval_ms": 100,
    "slaves": [
        {"enabled": True, "host": "127.0.0.1", "port": S1_PORT, "unit_id": 1, "registers": [
            {"addr": 0, "func": 3, "key": "temperature", "scale": 0.1, "period_ms": 300,
             "signed": False, "digits": 2, "writable": False, "product": ""},
            {"addr": 1, "func": 3, "key": "humidity", "scale": 0.1, "period_ms": 1500,
             "signed": False, "digits": 2, "writable": False, "product": ""},
        ]},
        {"enabled": True, "host": "127.0.0.1", "port": S2_PORT, "unit_id": 2, "registers": [
            {"addr": 5, "func": 3, "key": "energy", "scale": 1.0, "period_ms": 700,
             "signed": False, "digits": 0, "writable": False, "product": ""},
            {"addr": 6, "func": 3, "key": "voltage", "scale": 0.1, "period_ms": 700,
             "signed": False, "digits": 1, "writable": False, "product": ""},
        ]},
    ],
}


# -------------------- 真实 Modbus TCP 从站服务器 --------------------
class ModbusTCPSlave:
    """单连接 Modbus TCP 从站仿真：读保持寄存器(03)/写保持寄存器(06)。

    down=True 时对已连接/新连接只收不发（模拟网络黑洞 → 客户端超时重试），
    regs/count 可由测试线程直接读写。
    """

    def __init__(self, port, unit_id, regs):
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.regs = dict(regs)          # addr -> value
        self.count = {}                 # addr -> 读请求次数
        self.down = False
        self._stop = False
        self._conns = []
        self._conn_lock = threading.Lock()
        self._srv = real_socket.socket(real_socket.AF_INET, real_socket.SOCK_STREAM)
        self._srv.setsockopt(real_socket.SOL_SOCKET, real_socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.port))
        self._srv.listen(4)
        self._srv.settimeout(0.5)

    def log(self, msg):
        print("[SLAVE-%d] %s" % (self.port, msg), flush=True)

    def _serve(self, conn):
        conn.settimeout(1.0)
        try:
            while not self._stop:
                if self.down:
                    real_time.sleep(0.05)
                    continue
                header = b""
                while len(header) < 7:
                    chunk = conn.recv(7 - len(header))
                    if not chunk:
                        return
                    header += chunk
                if len(header) < 7:
                    return
                trans, proto, plen, unit = struct.unpack(">HHHB", header)
                pdu = b""
                while len(pdu) < plen - 1:
                    chunk = conn.recv(plen - 1 - len(pdu))
                    if not chunk:
                        return
                    pdu += chunk
                if len(pdu) < 1:
                    return
                func = pdu[0]
                if func == 3 and len(pdu) == 5:
                    addr = struct.unpack(">H", pdu[1:3])[0]
                    qty = struct.unpack(">H", pdu[3:5])[0]
                    self.count[addr] = self.count.get(addr, 0) + 1
                    if qty != 1 or addr not in self.regs:
                        resp_pdu = struct.pack(">BB", func | 0x80, 0x02)
                    else:
                        resp_pdu = struct.pack(">BBH", func, 2, self.regs[addr] & 0xFFFF)
                    resp = struct.pack(">HHHB", trans, proto, len(resp_pdu) + 1, unit) + resp_pdu
                    conn.sendall(resp)
                elif func == 6 and len(pdu) == 5:
                    addr = struct.unpack(">H", pdu[1:3])[0]
                    val = struct.unpack(">H", pdu[3:5])[0]
                    self.regs[addr] = val
                    resp = struct.pack(">HHHB", trans, proto, len(pdu) + 1, unit) + pdu
                    conn.sendall(resp)
                    self.log("write addr=0x%04x val=%d" % (addr, val))
                else:
                    resp_pdu = struct.pack(">BB", func | 0x80, 0x01)
                    resp = struct.pack(">HHHB", trans, proto, len(resp_pdu) + 1, unit) + resp_pdu
                    conn.sendall(resp)
        except (OSError, Exception):
            pass
        finally:
            with self._conn_lock:
                try:
                    conn.close()
                except Exception:
                    pass

    def _accept_loop(self):
        self.log("listening 127.0.0.1:%d (unit=%d)" % (self.port, self.unit_id))
        while not self._stop:
            try:
                conn, addr = self._srv.accept()
            except real_socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            with self._conn_lock:
                self._conns.append(conn)
            t.start()

    def start(self):
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass
        with self._conn_lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


def main():
    ap = argparse.ArgumentParser(description="4路继电器+Modbus TCP 网关固件 PC 仿真验收")
    ap.add_argument("--backend", choices=["auto", "real", "loopback"], default="auto")
    ap.add_argument("--mqtt-host", default=None)
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--web-port", type=int, default=18082)
    ap.add_argument("--fw", default=os.path.join(FW_DIR, "main.py"))
    args = ap.parse_args()

    lb = R.LogBuffer(sys.stdout)
    sys.stdout = lb

    mqtt_host, mqtt_port, mqtt_user, mqtt_pass = args.mqtt_host or "", args.mqtt_port or 0, args.mqtt_user or "", args.mqtt_pass or ""
    try:
        import config as proj_cfg
        mqtt_host = mqtt_host or proj_cfg.MQTT_HOST
        mqtt_port = mqtt_port or proj_cfg.MQTT_PORT
        mqtt_user = mqtt_user or proj_cfg.MQTT_USER
        mqtt_pass = mqtt_pass or proj_cfg.MQTT_PASS
    except Exception as e:
        log("SIM", "warning: 读取 config.py 失败(%s)" % e)

    backend_mode = args.backend
    broker_ok = bool(mqtt_host) and R.broker_reachable(mqtt_host, mqtt_port)
    if backend_mode == "auto":
        backend_mode = "real" if broker_ok else "loopback"
    log("SIM", "backend=%s broker=%s:%s reachable=%s" % (backend_mode, mqtt_host, mqtt_port, broker_ok))

    # 每次运行使用全新子目录作为“设备 flash”（首启无 config.json -> 进配网模式；
    # 独立目录避免删除残留文件触发沙箱回收站限制）
    run_dir = os.path.join(FLASH_DIR, "run-%d" % int(real_time.time() * 1000))
    os.makedirs(run_dir, exist_ok=True)
    os.chdir(run_dir)
    cfg_path = os.path.join(run_dir, CONFIG_NAME)
    # v5.2+ 配网页外置为 flash 的 portal.html，固件渲染时读该文件；预置源文件
    import shutil
    shutil.copyfile(os.path.join(FW_DIR, "portal_page.html"),
                    os.path.join(run_dir, "portal.html"))

    stubs = R.inject_stubs(web_port=args.web_port)
    machine = stubs["machine"]
    uq = stubs["umqtt"]

    # 真实 TCP 从站（独立于固件线程）
    s1 = ModbusTCPSlave(S1_PORT, 1, S1_REGS)
    s2 = ModbusTCPSlave(S2_PORT, 2, S2_REGS)
    s1.start()
    s2.start()

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        log("RESULT", ("PASS  " if ok else "FAIL  ") + name + ((" | " + str(detail)) if detail else ""))

    base = "/%s/%s" % (PRODUCT_ID, DEVICE_ID)

    # 后端
    backend = None
    if backend_mode == "real" and broker_ok:
        try:
            backend = R.RealBackend(mqtt_host, mqtt_port, mqtt_user, mqtt_pass)
        except Exception as e:
            log("SIM", "real backend 连接失败(%s)，退回 loopback" % e)
            backend = None
    if backend is None:
        backend = R.LoopBackBackend(uq)
        log("SIM", "使用 loopback 后端")

    fw = R.FirmwareRunner(os.path.abspath(args.fw))
    try:
        # ============ A. 首启 -> 配网页 ============
        log("PHASE", "A. 首次启动(无配置) -> 配网热点 + Web 配置页")
        fw.start()
        resp = R.wait_http_ready(args.web_port, timeout_s=25, mark=b"Modbus TCP")
        check("A1 配网页可访问(TCP 标题)", resp is not None)
        check("A2 页面含 Modbus JSON 配置区",
              resp is not None and b"modbus_json" in resp and b"slaves" in resp)

        # ============ B. 提交 TCP 配置 -> 重启 ============
        log("PHASE", "B. 提交 Modbus TCP 配置(2从站4寄存器)并重启")
        save_cfg = {
            "wifi_ssid": "sim-ap",
            "mqtt_host": mqtt_host if backend_mode == "real" and broker_ok else "127.0.0.1",
            "mqtt_port": mqtt_port if backend_mode == "real" and broker_ok else 1,
            "mqtt_user": mqtt_user if backend_mode == "real" and broker_ok else DEVICE_ID,
            "mqtt_password": mqtt_pass or "",
            "device_id": DEVICE_ID,
        }
        body = R.build_save_form(save_cfg, MODBUS_JSON)
        resp = R.http_request(args.web_port, "/save", method="POST", body=body)
        first = resp.split(b"\r\n", 1)[0] if resp else b""
        check("B1 保存配置返回 200", b"200" in first)
        real_time.sleep(3.5)
        check("B2 设备发生重启(boot>=2)", fw.boot_count >= 2, "boot=%d" % fw.boot_count)
        cfg_ok, detail = False, ""
        if os.path.exists(cfg_path):
            try:
                saved = json.load(open(cfg_path, "r", encoding="utf-8"))
                mb = saved.get("modbus") or {}
                sl = mb.get("slaves", [])
                cfg_ok = (mb.get("mode") == "tcp" and len(sl) == 2
                          and sl[0].get("host") == "127.0.0.1" and sl[0].get("port") == S1_PORT
                          and sl[1].get("port") == S2_PORT
                          and sl[0]["registers"][0].get("addr") == 0
                          and sl[0]["registers"][0].get("key") == "temperature"
                          and sl[0]["registers"][0].get("period_ms") == 300)
                detail = "mode=%s slaves=%d regs=%d/%d" % (mb.get("mode"), len(sl),
                                                           len(sl[0]["registers"]) if sl else -1,
                                                           len(sl[1]["registers"]) if len(sl) > 1 else -1)
            except Exception as e:
                detail = "parse err %s" % e
        check("B3 config.json 持久化 TCP 配置(2从站/地址/key/周期)",
              cfg_ok, detail)

        # ============ C. 需求1+3: TCP 采集 -> 属性合并上报(数值换算) ============
        log("PHASE", "C. 需求1/3: 从站+地址+key 采集，sN_ 与裸 key 合并上报")
        start_ok = R.wait_log(lb, "start_modbus mode=tcp result: True", timeout_s=20)
        check("C1 TCP 主站线程启动成功", start_ok)
        conn_ok = R.wait_log(lb, "slave[0] connected 127.0.0.1:%d" % S1_PORT, timeout_s=10) and \
            R.wait_log(lb, "slave[1] connected 127.0.0.1:%d" % S2_PORT, timeout_s=10)
        check("C2 两个从站 TCP 连接建立(host/port/unit)", conn_ok)

        prop = backend.wait_property(timeout_s=30)
        check("C3 收到首条属性上报", prop is not None)
        mb_prop, idx0 = None, backend.msg_count()
        t0 = real_time.time()
        while real_time.time() - t0 < 25:
            p = backend.wait_property(timeout_s=5, from_index=idx0)
            if p is None:
                break
            ps = p.get("properties", {})
            if "s1_temperature" in ps and "s2_energy" in ps:
                mb_prop = p
                break
            idx0 = backend.msg_count()
        ok = mb_prop is not None
        detail = ""
        if mb_prop:
            ps = mb_prop["properties"]
            detail = ("s1_t=%s s1_h=%s s2_e=%s s2_v=%s | bare t=%s e=%s"
                      % (ps.get("s1_temperature"), ps.get("s1_humidity"),
                         ps.get("s2_energy"), ps.get("s2_voltage"),
                         ps.get("temperature"), ps.get("energy")))
            ok = (ps.get("s1_temperature") == 25.0 and ps.get("s1_humidity") == 45.6
                  and ps.get("s2_energy") == 1234 and ps.get("s2_voltage") == 220.0
                  and ps.get("temperature") == 25.0 and ps.get("energy") == 1234)
        check("C4 需求1: 寄存器0x0000->temperature 等 key 映射正确且换算/合并上报", ok, detail)
        log_ok = (lb.contains("slave[0] addr=0x0000 key=temperature raw=250 value=25.0")
                  and lb.contains("slave[0] addr=0x0001 key=humidity raw=456 value=45.6")
                  and lb.contains("slave[1] addr=0x0005 key=energy raw=1234 value=1234"))
        check("C5 [MODBUS-TCP]关键日志(从站下标/0x地址/key/raw/value)", log_ok)

        # ============ D. 需求2: 同从站多地址独立采集周期 ============
        log("PHASE", "D. 需求2: 多采集地址独立周期(addr0=300ms vs addr1=1500ms)")
        s1.count.clear()
        real_time.sleep(5.2)
        c_fast, c_slow = s1.count.get(0, 0), s1.count.get(1, 0)
        detail = "5.2s 内 addr0(300ms)=%d 次, addr1(1500ms)=%d 次" % (c_fast, c_slow)
        check("D1 快周期地址采集次数显著多于慢周期", c_fast >= 2 * c_slow + 2 and c_fast >= 8, detail)
        check("D2 慢周期地址仍按自己的节奏持续采集", c_slow >= 2, "c_slow=%d" % c_slow)

        # ============ E. 需求3: 从站故障隔离 + 恢复 ============
        log("PHASE", "E. 需求3: 从站2故障不影响从站1；恢复后新值生效")
        s2.down = True
        err_ok = R.wait_log(lb, "slave[1] addr=0x0005 key=energy err=", timeout_s=12)
        check("E1 从站2 掉线出现错误日志(超时重试)", err_ok)
        s1.regs[0] = 350          # 从站1 寄存器0 -> 35.0
        idx = backend.msg_count()
        got = False
        t0 = real_time.time()
        while real_time.time() - t0 < 15:
            p = backend.wait_property(timeout_s=5, from_index=idx)
            if p is None:
                break
            if p.get("properties", {}).get("s1_temperature") == 35.0:
                got = True
                break
            idx = backend.msg_count()
        check("E2 从站2 故障期间从站1 采集/上报不受影响(350->35.0)", got)
        s2.regs[5] = 5678
        s2.down = False
        idx = backend.msg_count()
        got2 = False
        t0 = real_time.time()
        while real_time.time() - t0 < 15:
            p = backend.wait_property(timeout_s=5, from_index=idx)
            if p is None:
                break
            if p.get("properties", {}).get("s2_energy") == 5678:
                got2 = True
                break
            idx = backend.msg_count()
        check("E3 从站2 恢复后新值生效(5678)", got2)

        # ============ F. 需求4: 采集不阻塞继电器（核心） ============
        log("PHASE", "F. 需求4: 从站1假死(黑洞)期间继电器命令秒回/吸合/事件上报")
        s1.down = True
        err1 = R.wait_log(lb, "slave[0] addr=0x0000 key=temperature err=", timeout_s=15)
        check("F0 从站1 假死后采集线程进入超时重试", err1)

        mid = "tcp-cmd-001"
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
        check("F1 假死从站期间命令仍秒回(%.2fs<3s)" % elapsed,
              ok_reply and elapsed < 3.0, "elapsed=%.2fs" % elapsed)
        check("F2 ch1 继电器吸合(GPIO3=0)", machine.sim_read(3) == 0,
              "GPIO3=%s" % machine.sim_read(3))
        ev = backend.wait_topic(base + "/event/switch_change", timeout_s=8)
        check("F3 switch_change 事件上报", ev is not None)
        idx = backend.msg_count()
        prop2 = backend.wait_property(timeout_s=10, from_index=idx)
        ch1_on = prop2 is not None and prop2.get("properties", {}).get("ch1_state") is True
        check("F4 假死期间属性上报仍正常(含 ch1_state=true)", ch1_on)

        # 再验证从站2 也假死时 ch2 关闭命令仍秒回（多从站全挂的最坏情况）
        s2.down = True
        real_time.sleep(0.5)
        mid2 = "tcp-cmd-002"
        t_start = real_time.time()
        backend.downlink(base + "/service/cmd", json.dumps({
            "functionId": "set_channel", "messageId": mid2,
            "inputs": [{"name": "channel", "value": 2}, {"name": "state", "value": False}]}))
        got2, pl2 = backend.wait_topic(base + "/function/post", timeout_s=12,
                                       predicate=lambda t, p: mid2 in p)
        elapsed2 = real_time.time() - t_start
        ok2 = bool(got2) and json.loads(pl2).get("success") is True
        check("F5 双从站全挂时命令仍秒回(%.2fs<3s)" % elapsed2, ok2 and elapsed2 < 3.0,
              "elapsed=%.2fs" % elapsed2)
        check("F6 ch2 继电器断开(GPIO4=1)", machine.sim_read(4) == 1,
              "GPIO4=%s" % machine.sim_read(4))

        # 恢复两个从站 -> 采集值继续刷新
        s1.down = False
        s2.down = False
        idx = backend.msg_count()
        got3 = False
        t0 = real_time.time()
        while real_time.time() - t0 < 15:
            p = backend.wait_property(timeout_s=5, from_index=idx)
            if p is None:
                break
            ps = p.get("properties", {})
            if ps.get("s1_temperature") == 35.0 and ps.get("s2_energy") == 5678:
                got3 = True
                break
            idx = backend.msg_count()
        check("F7 从站恢复后采集自动续传(35.0/5678)", got3)

    finally:
        fw.stop()
        backend.close()
        s1.stop()
        s2.stop()
        sys.stdout = getattr(sys.stdout, "_real", sys.stdout)

    passed = sum(1 for x in results if x)
    log("SUMMARY", "PASS %d / %d" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
