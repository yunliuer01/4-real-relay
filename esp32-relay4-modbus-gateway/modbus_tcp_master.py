# -*- coding: utf-8 -*-
"""Modbus TCP 采集网关主站（MicroPython / ESP32-C3）

特性：
- 作为 Modbus TCP 客户端，采集多个服务器/从站的保持/输入寄存器
- 独立 _thread 线程运行，通过 latest_values 共享数据，不阻塞继电器主循环
- 支持多从站，每个从站独立配置 host/port/unit_id
- 支持每个从站下挂多个寄存器，每个寄存器独立采集周期、上报 key、缩放、小数位
- 保留 Modbus RTU 的 key 命名风格（sN_key），与现有属性上报兼容
- 连接保持复用，失败时自动重连
"""
import struct
import time
import socket

try:
    import _thread
except ImportError:
    _thread = None


class ModbusTCPMaster:
    def __init__(self, config):
        self.cfg = config
        self.enabled = bool(config.get("enabled", True))
        self.timeout_ms = int(config.get("timeout_ms", 500))
        self.retries = int(config.get("retries", 2))
        self.retry_interval_ms = int(config.get("retry_interval_ms", 500))
        # slaves 列表：每个元素包含 host, port, unit_id, enabled, registers
        self.slaves = config.get("slaves", [])

        self.latest_values = {}
        self._lock = None
        self._running = False
        self._thread_id = None
        self._error_counts = {}
        self._cycle_counts = {}
        self._last_poll = {}
        self._conns = {}   # slave_idx -> socket（复用连接）
        self._trans_id = 0

    def _log(self, level, msg):
        """统一日志输出：带时间戳和模块名"""
        t = time.localtime()
        ts = "%04d-%02d-%02dT%02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
        print("[MODBUS-TCP][%s][%s] %s" % (level, ts, msg))

    def init_hw(self):
        """TCP 无需 UART/RS485 硬件初始化，仅分配锁"""
        if not self.enabled:
            self._log("INFO", "disabled by config")
            return True
        self._lock = _thread.allocate_lock() if _thread else None
        self._log("INFO", "init ok, slaves=%d" % len(self.slaves))
        return True

    def _next_trans_id(self):
        self._trans_id = (self._trans_id + 1) & 0xFFFF
        return self._trans_id

    def _connect(self, slave_idx, slave_cfg):
        """建立到指定从站的 TCP 连接；优先复用已有连接，失败则重建"""
        host = slave_cfg.get("host", "127.0.0.1")
        port = int(slave_cfg.get("port", 502))
        if self._conns.get(slave_idx) is not None:
            return self._conns[slave_idx]

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout_ms / 1000.0)
            sock.connect((host, port))
            self._conns[slave_idx] = sock
            self._log("INFO", "slave[%d] connected %s:%d" % (slave_idx, host, port))
            return sock
        except Exception as e:
            self._log("WARN", "slave[%d] connect %s:%d failed: %s" % (slave_idx, host, port, e))
            return None

    def _send_recv(self, slave_idx, slave_cfg, pdu):
        """发送 Modbus PDU 并接收响应；返回 (response_pdu, err)"""
        unit_id = int(slave_cfg.get("unit_id", 1))
        sock = self._connect(slave_idx, slave_cfg)
        if sock is None:
            return None, "connect failed"

        trans_id = self._next_trans_id()
        length = len(pdu) + 1
        mbap = struct.pack(">HHHB", trans_id, 0, length, unit_id)
        frame = mbap + pdu

        last_err = None
        for attempt in range(self.retries):
            try:
                sock.settimeout(self.timeout_ms / 1000.0)
                sock.send(frame)

                # 读取 MBAP 头 7 字节
                header = b""
                while len(header) < 7:
                    chunk = sock.recv(7 - len(header))
                    if not chunk:
                        raise OSError("connection closed")
                    header += chunk

                recv_trans, proto, resp_len, recv_unit = struct.unpack(">HHHB", header)
                # 读取剩余 PDU
                pdu_len = resp_len - 1
                pdu_data = b""
                while len(pdu_data) < pdu_len:
                    chunk = sock.recv(pdu_len - len(pdu_data))
                    if not chunk:
                        raise OSError("connection closed")
                    pdu_data += chunk

                if recv_trans != trans_id:
                    return None, "transaction id mismatch"
                if recv_unit != unit_id:
                    return None, "unit id mismatch"
                return pdu_data, None

            except Exception as e:
                last_err = str(e)
                self._log("WARN", "slave[%d] attempt %d/%d err: %s" %
                          (slave_idx, attempt + 1, self.retries, last_err))
                # 关闭并重连
                try:
                    sock.close()
                except Exception:
                    pass
                self._conns[slave_idx] = None
                if attempt + 1 < self.retries:
                    time.sleep_ms(self.retry_interval_ms)
                    sock = self._connect(slave_idx, slave_cfg)
                    if sock is None:
                        return None, "reconnect failed"

        return None, last_err

    def read_register(self, slave_idx, slave_cfg, addr, func_code=3):
        """读取单个 16bit 寄存器；返回 (value, None) 或 (None, err)"""
        pdu = struct.pack(">BHH", func_code, addr, 1)
        resp, err = self._send_recv(slave_idx, slave_cfg, pdu)
        if err:
            return None, err
        if len(resp) < 4:
            return None, "short response"
        if resp[0] & 0x80:
            return None, "modbus exception 0x%02x" % resp[1]
        byte_count = resp[1]
        if byte_count != 2:
            return None, "unexpected byte count %d" % byte_count
        value = struct.unpack(">H", resp[2:4])[0]
        return value, None

    def write_register(self, slave_idx, slave_cfg, addr, value):
        """写单个保持寄存器（功能码 06）"""
        pdu = struct.pack(">BHH", 6, addr, int(value) & 0xFFFF)
        resp, err = self._send_recv(slave_idx, slave_cfg, pdu)
        if err:
            return False, err
        if len(resp) < 5:
            return False, "short response"
        if resp[0] != 6:
            if resp[0] & 0x80:
                return False, "modbus exception 0x%02x" % resp[1]
            return False, "function code mismatch"
        return True, None

    def _update_value(self, slave_idx, key, value):
        """线程安全地更新 latest_values"""
        full_key = "s%d_%s" % (slave_idx + 1, key)
        if self._lock:
            self._lock.acquire()
        self.latest_values[full_key] = value
        if self._lock:
            self._lock.release()

    def get_values(self):
        """主线程调用：安全拷贝当前采集值"""
        if self._lock:
            self._lock.acquire()
        out = dict(self.latest_values)
        if self._lock:
            self._lock.release()
        return out

    def _poll_slave(self, slave_idx, slave_cfg):
        """轮询一个 TCP 从站的所有寄存器"""
        if not slave_cfg.get("enabled", True):
            return
        registers = slave_cfg.get("registers", [])
        now = time.ticks_ms()

        sid = str(slave_idx)
        if sid not in self._last_poll:
            self._last_poll[sid] = {}
        if sid not in self._cycle_counts:
            self._cycle_counts[sid] = 0
        if sid not in self._error_counts:
            self._error_counts[sid] = 0

        polled_any = False
        for ri, reg in enumerate(registers):
            if not isinstance(reg, dict):
                continue
            addr = int(reg.get("addr", 0))
            func = int(reg.get("func", 3))
            key = str(reg.get("key", "reg_%d" % addr))
            scale = float(reg.get("scale", 1.0))
            period_ms = int(reg.get("period_ms", 1000))
            digits = int(reg.get("digits", 0))
            signed = bool(reg.get("signed", False))

            last = self._last_poll[sid].get(ri, 0)
            if time.ticks_diff(now, last) < period_ms:
                continue
            self._last_poll[sid][ri] = now
            polled_any = True

            raw, err = self.read_register(slave_idx, slave_cfg, addr, func)
            if err is not None:
                self._error_counts[sid] += 1
                self._log("ERROR", "slave[%d] addr=0x%04x key=%s err=%s" %
                          (slave_idx, addr, key, err))
                continue

            if signed and raw > 32767:
                raw -= 65536
            value = round(raw * scale, digits)
            self._update_value(slave_idx, key, value)
            self._log("INFO", "slave[%d] addr=0x%04x key=%s raw=%d value=%s" %
                      (slave_idx, addr, key, raw, value))

        if polled_any:
            self._cycle_counts[sid] += 1

    def _worker(self):
        """独立线程入口"""
        self._log("INFO", "worker thread started")
        while self._running:
            try:
                for si, slave in enumerate(self.slaves):
                    if not self._running:
                        break
                    self._poll_slave(si, slave)
                    # 从站之间稍作礼让，给继电器/MQTT 留出 CPU
                    time.sleep_ms(10)
            except Exception as e:
                self._log("ERROR", "worker exception: %s" % e)
            # 主循环节拍 50ms
            time.sleep_ms(50)
        # 清理所有 TCP 连接
        for sock in self._conns.values():
            try:
                sock.close()
            except Exception:
                pass
        self._conns.clear()
        self._log("INFO", "worker thread stopped")

    def start(self):
        """启动采集线程"""
        if not self.enabled:
            self._log("INFO", "not starting (disabled)")
            return False
        if not _thread:
            self._log("ERROR", "_thread module not available")
            return False
        if not self.init_hw():
            return False
        if self._running:
            return True
        self._running = True
        self._thread_id = _thread.start_new_thread(self._worker, ())
        self._log("INFO", "started, thread_id=%s" % str(self._thread_id))
        return True

    def stop(self):
        """停止采集线程并关闭所有连接"""
        self._running = False
        time.sleep_ms(200)
        for sock in self._conns.values():
            try:
                sock.close()
            except Exception:
                pass
        self._conns.clear()
        self._log("INFO", "stopped")
