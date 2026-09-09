# -*- coding: utf-8 -*-
"""CPython 集成测试：起一个简易 Modbus TCP 从站，验证 ModbusTCPMaster 能正确读取。"""
import sys
import time
import socket
import struct
import threading

# 补齐 MicroPython time API
if not hasattr(time, "ticks_ms"):
    _base = time.monotonic()
    time.ticks_ms = lambda: int((time.monotonic() - _base) * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)

sys.path.insert(0, r"D:\8-relay\esp32-relay4-modbus-gateway")
from modbus_tcp_master import ModbusTCPMaster

HOST = "127.0.0.1"
PORT = 15502  # 避免与真实 5502 冲突
UNIT_ID = 4
REGISTERS = {3: 440, 4: 445}


def simple_server():
    """极简 Modbus TCP 从站：只响应读保持寄存器 06 功能码 03"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(1)
    srv.settimeout(1.0)
    print("[SERVER] listening %s:%d" % (HOST, PORT))
    conn = None
    try:
        conn, addr = srv.accept()
        print("[SERVER] client from", addr)
        conn.settimeout(5.0)
        while True:
            # 读 MBAP
            header = b""
            while len(header) < 7:
                chunk = conn.recv(7 - len(header))
                if not chunk:
                    return
                header += chunk
            trans_id, proto, pdu_len, unit_id = struct.unpack(">HHHB", header)
            # 读 PDU
            pdu = b""
            while len(pdu) < pdu_len - 1:
                chunk = conn.recv(pdu_len - 1 - len(pdu))
                if not chunk:
                    return
                pdu += chunk
            func = pdu[0]
            if func == 3 and len(pdu) == 5:
                addr = struct.unpack(">H", pdu[1:3])[0]
                qty = struct.unpack(">H", pdu[3:5])[0]
                vals = []
                for i in range(qty):
                    vals.append(REGISTERS.get(addr + i, 0))
                resp_pdu = struct.pack(">BB", func, qty * 2) + b"".join(struct.pack(">H", v) for v in vals)
                resp_mbap = struct.pack(">HHHB", trans_id, proto, len(resp_pdu) + 1, unit_id)
                conn.send(resp_mbap + resp_pdu)
                print("[SERVER] read addr=%d qty=%d -> %s" % (addr, qty, vals))
            elif func == 6 and len(pdu) == 5:
                addr = struct.unpack(">H", pdu[1:3])[0]
                val = struct.unpack(">H", pdu[3:5])[0]
                REGISTERS[addr] = val
                resp_mbap = struct.pack(">HHHB", trans_id, proto, len(pdu) + 1, unit_id)
                conn.send(resp_mbap + pdu)
                print("[SERVER] write addr=%d val=%d" % (addr, val))
            else:
                # 异常响应
                resp_pdu = struct.pack(">BB", func | 0x80, 1)
                resp_mbap = struct.pack(">HHHB", trans_id, proto, len(resp_pdu) + 1, unit_id)
                conn.send(resp_mbap + resp_pdu)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        srv.close()


def main():
    server_thread = threading.Thread(target=simple_server, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    cfg = {
        "enabled": True,
        "mode": "tcp",
        "timeout_ms": 1000,
        "retries": 1,
        "retry_interval_ms": 100,
        "slaves": [
            {
                "enabled": True,
                "host": HOST,
                "port": PORT,
                "unit_id": UNIT_ID,
                "registers": [
                    {"addr": 3, "func": 3, "key": "temperature", "period_ms": 500, "scale": 0.1, "digits": 1, "signed": False},
                    {"addr": 4, "func": 3, "key": "humidity", "period_ms": 500, "scale": 1, "digits": 0, "signed": False},
                ],
            }
        ],
    }
    master = ModbusTCPMaster(cfg)
    ok = master.init_hw() and master.start()
    print("[TEST] master start:", ok)

    # 等待至少一轮采集
    time.sleep(1.5)
    values = master.get_values()
    print("[TEST] values:", values)

    assert "s1_temperature" in values, "missing s1_temperature"
    assert "s1_humidity" in values, "missing s1_humidity"
    assert abs(values["s1_temperature"] - 44.0) < 0.01, values["s1_temperature"]
    assert values["s1_humidity"] == 445, values["s1_humidity"]

    # 测试写寄存器
    write_ok, err = master.write_register(0, cfg["slaves"][0], 3, 999)
    print("[TEST] write ok:", write_ok, "err:", err)
    assert write_ok, err
    assert REGISTERS[3] == 999

    master.stop()
    print("[TEST] all assertions passed")


if __name__ == "__main__":
    main()
