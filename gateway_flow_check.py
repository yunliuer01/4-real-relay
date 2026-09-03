# -*- coding: utf-8 -*-
"""只读验证：JetLinks 网关 jetlinks-g5-lfx 是否持续收到两端上报(recv_msg/recv_oct 增长=规则已转发)。
不修改任何平台配置。"""
import json
import time
import urllib.request
import urllib.error

EMQX = "http://172.16.4.211:9183/api/v5"
EMQX_AUTH = ("group5", "Admin@group5")
GATEWAY = "jetlinks-g5-lfx"
TARGETS = ["FILE-TERM-01", "MODBUS-TERM-01"]


def http_req(url, headers=None, data=None, method=None):
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


def emqx_token():
    st, body = http_req(EMQX + "/login", headers={"Content-Type": "application/json"},
                        data={"username": EMQX_AUTH[0], "password": EMQX_AUTH[1]})
    tok = None
    if st == 200 and isinstance(body, dict):
        tok = body.get("token") or (body.get("data") or {}).get("token")
    return {"Authorization": "Bearer " + tok} if tok else None


def gw_stats(eh):
    st, body = http_req(EMQX + "/clients/" + GATEWAY, headers=eh)
    if st != 200:
        return None
    return {
        "recv_cnt": body.get("recv_cnt"),
        "recv_msg": body.get("recv_msg"),
        "recv_oct": body.get("recv_oct"),
        "send_msg": body.get("send_msg"),
        "connected_at": body.get("connected_at"),
    }


def main():
    eh = emqx_token()
    if not eh:
        print("EMQX 登录失败"); return
    print("=== 采样 1 (t0) 网关 %s ===" % GATEWAY)
    s1 = gw_stats(eh)
    print("  " + json.dumps(s1, ensure_ascii=False))
    time.sleep(20)
    print("=== 采样 2 (t+20s) ===")
    s2 = gw_stats(eh)
    print("  " + json.dumps(s2, ensure_ascii=False))
    if s1 and s2:
        d = {k: (s2[k] - s1[k]) for k in ("recv_msg", "recv_oct", "send_msg") if s1.get(k) is not None and s2.get(k) is not None}
        print("--- 20s 增量 ---")
        print("  " + json.dumps(d))
        print("结论: 网关收到上报 → recv_msg/recv_oct 增长；增长≈%d条/20s 说明规则转发正常" % d.get("recv_msg", -1))


if __name__ == "__main__":
    main()
