# -*- coding: utf-8 -*-
"""只读诊断：列出当前所有与终端相关(TERM/192.168.30.80)的 EMQX 客户端，
带订阅主题与收发计数，两次采样确认活跃度。不修改任何配置。"""
import json
import time
import urllib.request
import urllib.error

EMQX = "http://172.16.4.211:9183/api/v5"
EMQX_AUTH = ("group5", "Admin@group5")


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


def list_clients(tok):
    h = {"Authorization": "Bearer " + tok}
    out = []
    for page in range(0, 500, 100):
        st, body = req(EMQX + f"/clients?limit=100&cursor={page}", headers=h)
        # EMQX v5 用 cursor 不是数字，改走全量拉取
        break
    st, body = req(EMQX + "/clients?limit=500", headers=h)
    if st != 200:
        print("clients HTTP %s" % st)
        return out
    data = body.get("data", []) if isinstance(body, dict) else body
    for c in data:
        cid = c.get("clientid", "")
        ip = c.get("ip_address", "")
        if ("TERM" in cid.upper()) or (ip and ip.startswith("192.168.30")):
            out.append({
                "cid": cid, "ip": ip,
                "connected_at": c.get("connected_at", ""),
                "recv_cnt": c.get("recv_cnt"), "recv_msg": c.get("recv_msg"),
                "recv_oct": c.get("recv_oct"), "send_msg": c.get("send_msg"),
                "subs": c.get("subscriptions_count"),
            })
    return out


def dump(tok, tag):
    print("--- %s ---" % tag)
    for c in list_clients(tok):
        print("%-30s ip=%-16s connected_at=%s" % (c["cid"], c["ip"], c["connected_at"]))
        print("    recv_cnt=%-8s recv_msg=%-6s recv_oct=%-10s send_msg=%-8s subs=%s" % (
            c["recv_cnt"], c["recv_msg"], c["recv_oct"], c["send_msg"], c["subs"]))


def main():
    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": EMQX_AUTH[0], "password": EMQX_AUTH[1]})
    tok = body.get("token") if st == 200 else None
    if not tok:
        print("EMQX 登录失败: %s" % st)
        return
    dump(tok, "采样 1 (t0)")
    print("\n等待 35 秒 ...")
    time.sleep(35)
    dump(tok, "采样 2 (t+35s)")


if __name__ == "__main__":
    main()
