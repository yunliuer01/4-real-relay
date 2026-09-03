# -*- coding: utf-8 -*-
"""只读诊断：检查 FILE-TERM-01 / MODBUS-TERM-01 是否在线并持续上报。
间隔采样 recv_cnt/recv_msg/send_msg，观察是否增长。不修改任何配置。"""
import json
import time
import base64
import urllib.request
import urllib.error

EMQX = "http://172.16.4.211:9183/api/v5"
EMQX_AUTH = ("group5", "Admin@group5")
CANDIDATES = ["FILE-TERM-01", "FILE-TERM-01-9554a2", "MODBUS-TERM-01", "MODBUS-TERM-01-9554a2"]


def req(url, headers=None, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def snap(tok, cid):
    h = {"Authorization": "Bearer " + tok}
    st, d = req(EMQX + "/clients/" + cid, headers=h)
    if st != 200:
        return None
    return {
        "connected": d.get("connected"),
        "connected_at": d.get("connected_at", ""),
        "ip": d.get("ip_address"),
        "recv_cnt": d.get("recv_cnt"),
        "recv_msg": d.get("recv_msg"),
        "recv_oct": d.get("recv_oct"),
        "send_msg": d.get("send_msg"),
        "subs": d.get("subscriptions_count"),
    }


def main():
    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": EMQX_AUTH[0], "password": EMQX_AUTH[1]})
    tok = body.get("token") if st == 200 else None
    if not tok:
        print("EMQX 登录失败: %s" % st)
        return

    print("=== 采样 1 (t0) ===")
    alive = []
    for cid in CANDIDATES:
        s = snap(tok, cid)
        if s:
            alive.append(cid)
            print("%-26s connected=%s ip=%-16s recv_cnt=%-8s recv_msg=%-6s recv_oct=%-10s send_msg=%-8s subs=%s" % (
                cid, s["connected"], s["ip"], s["recv_cnt"], s["recv_msg"],
                s["recv_oct"], s["send_msg"], s["subs"]))
        else:
            print("%-26s (未连接)" % cid)

    if not alive:
        print("没有任何候选客户端在线 —— 两个终端都未连上 EMQX！")
        return

    print("\n等待 35 秒后二次采样，观察计数是否增长 ...")
    time.sleep(35)

    print("=== 采样 2 (t+35s) ===")
    for cid in alive:
        s = snap(tok, cid)
        if s:
            print("%-26s connected=%s ip=%-16s recv_cnt=%-8s recv_msg=%-6s recv_oct=%-10s send_msg=%-8s subs=%s" % (
                cid, s["connected"], s["ip"], s["recv_cnt"], s["recv_msg"],
                s["recv_oct"], s["send_msg"], s["subs"]))


if __name__ == "__main__":
    main()
