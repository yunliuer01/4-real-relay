# -*- coding: utf-8 -*-
"""Modbus RTU 主站采集器（MicroPython / ESP32-C3）

特性：
- 支持多从站、多寄存器独立配置采集周期
- 非阻塞：运行在独立线程，通过 latest_values 共享数据
- 自动 CRC16 校验、超时重试、错误计数
- 关键节点串口日志输出，便于现场排障

典型接线（TTL-RS485 模块）：
- RS485 TX  -> ESP32 UART RX (默认 IO21)
- RS485 RX  -> ESP32 UART TX (默认 IO20)
- RS485 DE/RE -> 方向控制 GPIO (默认 IO8)
- RS485 VCC -> 3.3V/5V, GND -> GND
"""
import struct
import time
import machine
from machine import Pin, UART

try:
    import _thread
except ImportError:
    _thread = None


# -------------------- CRC16 --------------------
def _crc16(data):
    """Modbus RTU CRC16，初始 0xFFFF，多项式 0xA001（反向 0x8005）"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _crc_bytes(data):
    crc = _crc16(data)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# -------------------- Modbus 主站 --------------------
class ModbusMaster:
    def __init__(self, config):
        self.cfg = config
        self.enabled = bool(config.get("enabled", True))
        self.uart_id = int(config.get("uart_id", 1))
        self.baudrate = int(config.get("baudrate", 9600))
        self.tx_pin = int(config.get("tx_pin", 20))
        self.rx_pin = int(config.get("rx_pin", 21))
        self.dir_pin = config.get("dir_pin")
        self.timeout_ms = int(config.get("timeout_ms", 500))
        self.slaves = config.get("slaves", [])
        self.retries = int(config.get("retries", 2))
        self.retry_interval_ms = int(config.get("retry_interval_ms", 500))

        self.uart = None
        self.dir = None
        self.latest_values = {}
        self._lock = None
        self._running = False
        self._thread_id = None
        self._error_counts = {}
        self._cycle_counts = {}
        self._last_poll = {}

    def _log(self, level, msg):
        """统一日志输出：带时间戳和模块名"""
        t = time.localtime()
        ts = "%04d-%02d-%02dT%02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
        print("[MODBUS][%s][%s] %s" % (level, ts, msg))

    def init_hw(self):
        """初始化 UART 和方向控制引脚"""
        if not self.enabled:
            self._log("INFO", "Modbus master disabled by config")
            return True
        try:
            # 注意：ESP32-C3 UART1 可用引脚较多，按配置绑定 TX/RX
            self.uart = UART(
                self.uart_id,
                baudrate=self.baudrate,
                tx=self.tx_pin,
                rx=self.rx_pin,
                bits=8,
                parity=None,
                stop=1,
                timeout=self.timeout_ms,
            )
            self._log("INFO", "UART%d init @ %d baud, tx=IO%d rx=IO%d" %
                      (self.uart_id, self.baudrate, self.tx_pin, self.rx_pin))
        except Exception as e:
            self._log("ERROR", "UART init failed: %s" % e)
            return False

        try:
            if self.dir_pin is not None:
                self.dir = Pin(int(self.dir_pin), Pin.OUT, value=0)
                self._log("INFO", "RS485 dir pin init IO%d (recv mode)" % int(self.dir_pin))
            else:
                self._log("INFO", "No RS485 dir pin configured")
        except Exception as e:
            self._log("WARN", "dir pin init failed: %s" % e)
            self.dir = None

        self._lock = _thread.allocate_lock() if _thread else None
        return True

    def _set_tx(self):
        if self.dir is not None:
            self.dir.value(1)
            time.sleep_us(20)

    def _set_rx(self):
        if self.dir is not None:
            time.sleep_us(80)
            self.dir.value(0)

    def _send(self, frame):
        """发送一帧，带方向切换"""
        self._set_tx()
        self.uart.write(frame)
        # 等待发送完成：1起始+8数据+1停止 = 10 bit
        tx_time_us = (1000000 * 10 * len(frame)) // self.baudrate
        time.sleep_us(max(100, tx_time_us + 500))
        self._set_rx()

    def _read_response(self, slave_id, func_code, expect_bytes):
        """读取 Modbus 响应帧，expect_bytes 为期望数据字节数（不含 CRC）"""
        # 最小帧：地址1 + 功能码1 + 字节数1 + 数据2 + CRC2 = 7
        expected_len = 3 + expect_bytes + 2
        # 增加一点余量，避免截断
        deadline = time.ticks_add(time.ticks_ms(), self.timeout_ms)
        buf = bytearray()
        while time.ticks_diff(time.ticks_ms(), deadline) < 0:
            if self.uart.any():
                buf += self.uart.read()
                if len(buf) >= expected_len:
                    break
            time.sleep_ms(5)
        # 再短暂等待尾部
        time.sleep_ms(int(max(1, (1000 * 10) // self.baudrate + 1)))
        if self.uart.any():
            buf += self.uart.read()

        if len(buf) < 5:
            return None, "timeout/short frame (got %d bytes)" % len(buf)

        # 检查地址和功能码
        if buf[0] != slave_id:
            return None, "slave id mismatch (got 0x%02x, want 0x%02x)" % (buf[0], slave_id)
        if buf[1] != func_code:
            # 异常响应：功能码最高位置1
            if buf[1] == (func_code | 0x80) and len(buf) >= 5:
                return None, "modbus exception 0x%02x" % buf[2]
            return None, "function code mismatch (got 0x%02x, want 0x%02x)" % (buf[1], func_code)

        # 校验 CRC
        payload = buf[:-2]
        recv_crc = buf[-2] | (buf[-1] << 8)
        calc_crc = _crc16(payload)
        if recv_crc != calc_crc:
            return None, "crc error (recv 0x%04x, calc 0x%04x)" % (recv_crc, calc_crc)

        return payload, None

    def read_register(self, slave_id, addr, func_code=3):
        """
        读取单个 16bit 寄存器。
        func_code=3 保持寄存器；func_code=4 输入寄存器。
        返回 (value, None) 或 (None, error_str)
        """
        if self.uart is None:
            return None, "uart not initialized"

        frame = struct.pack(">BBHH", slave_id, func_code, addr, 1)
        frame += _crc_bytes(frame)

        last_err = None
        for attempt in range(self.retries):
            self.uart.read()  # 清空 RX 缓存
            self._send(frame)
            payload, err = self._read_response(slave_id, func_code, 2)
            if err is None:
                try:
                    byte_count = payload[2]
                    if byte_count != 2:
                        return None, "unexpected byte count %d" % byte_count
                    value = struct.unpack(">H", payload[3:5])[0]
                    return value, None
                except Exception as e:
                    return None, "parse error: %s" % e
            last_err = err
            self._log("WARN", "slave=%d addr=0x%04x attempt=%d/%d err=%s" %
                      (slave_id, addr, attempt + 1, self.retries, err))
            if attempt + 1 < self.retries:
                time.sleep_ms(self.retry_interval_ms)
        return None, last_err

    def _update_value(self, slave_id, key, value):
        """线程安全地更新 latest_values"""
        full_key = "s%d_%s" % (slave_id, key)
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

    def _poll_slave(self, slave_cfg):
        """轮询一个从站的所有寄存器"""
        slave_id = int(slave_cfg.get("slave_id", 1))
        registers = slave_cfg.get("registers", [])
        now = time.ticks_ms()

        # 初始化该从站上次轮询时间
        sid = str(slave_id)
        if sid not in self._last_poll:
            self._last_poll[sid] = {}
        if sid not in self._cycle_counts:
            self._cycle_counts[sid] = 0
        if sid not in self._error_counts:
            self._error_counts[sid] = 0

        polled_any = False
        for reg in registers:
            if not isinstance(reg, dict):
                continue
            addr = int(reg.get("addr", 0))
            func = int(reg.get("func", 3))
            key = str(reg.get("key", "reg_%d" % addr))
            scale = float(reg.get("scale", 1.0))
            period_ms = int(reg.get("period_ms", 1000))

            last = self._last_poll[sid].get(addr, 0)
            if time.ticks_diff(now, last) < period_ms:
                continue
            self._last_poll[sid][addr] = now
            polled_any = True

            self._log("DEBUG", "poll slave=%d addr=0x%04x func=%d key=%s" %
                      (slave_id, addr, func, key))
            raw, err = self.read_register(slave_id, addr, func)
            if err is not None:
                self._error_counts[sid] += 1
                self._log("ERROR", "slave=%d addr=0x%04x key=%s err=%s" %
                          (slave_id, addr, key, err))
                continue

            # 有符号/无符号处理
            signed = bool(reg.get("signed", False))
            if signed and raw > 32767:
                raw -= 65536

            value = raw * scale
            # 保留小数位数
            digits = int(reg.get("digits", 2))
            value = round(value, digits)

            self._update_value(slave_id, key, value)
            self._log("INFO", "slave=%d addr=0x%04x key=%s raw=%d value=%s" %
                      (slave_id, addr, key, raw, value))

        if polled_any:
            self._cycle_counts[sid] += 1

    def _worker(self):
        """独立线程入口"""
        self._log("INFO", "Modbus worker thread started")
        while self._running:
            try:
                for slave_cfg in self.slaves:
                    if not self._running:
                        break
                    self._poll_slave(slave_cfg)
                    # 从站之间稍作礼让
                    time.sleep_ms(10)
            except Exception as e:
                self._log("ERROR", "worker exception: %s" % e)
            # 主循环节拍 50ms
            time.sleep_ms(50)
        self._log("INFO", "Modbus worker thread stopped")

    def start(self):
        """启动采集线程"""
        if not self.enabled:
            self._log("INFO", "not starting (disabled)")
            return False
        if not _thread:
            self._log("ERROR", "_thread module not available, cannot run in background")
            return False
        if self.uart is None and not self.init_hw():
            return False
        if self._running:
            return True
        self._running = True
        self._thread_id = _thread.start_new_thread(self._worker, ())
        self._log("INFO", "Modbus master started, thread_id=%s" % str(self._thread_id))
        return True

    def stop(self):
        self._running = False
        time.sleep_ms(200)
        if self.uart:
            try:
                self.uart.deinit()
            except Exception:
                pass
            self.uart = None
        self._log("INFO", "Modbus master stopped")
