# -*- coding: utf-8 -*-
"""JetLinks 网络组件 g5_mqtt_client 重启（shutdown->start），清除僵尸会话。
仅操作本组网络组件状态，不修改任何配置项。"""
import json
import time
import urllib.request
import urllib.error

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"
NET_ID = "2095085287379030016"
GW = "jetlinks-g5-lfx"


def req(url, headers=None, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def main():
    # JetLinks 登录
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    tok = (body.get("result") or {}).get("token") or body.get("token")
    jh = {"X-Access-Token": tok}
    print("[1] JetLinks login st=%s token=%s" % (st, "YES" if tok else "NO"))

    # EMQX 登录
    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": "group5", "password": "Admin@group5"})
    etok = body.get("token") or (body.get("data") or {}).get("token")
    eh = {"Authorization": "Bearer " + (etok or "")}

    def gw_state():
        st, b = req(EMQX + "/clients/" + GW, eh)
        if st == 200:
            return "connected_at=%s send_msg=%s recv_cnt=%s" % (b.get("connected_at"), b.get("send_msg"), b.get("recv_cnt"))
        return "EMQX 无此连接 (st=%s)" % st

    print("[2] 重启前:", gw_state())

    print("[3] shutdown 网络组件 ...")
    st, b = req(JL + "/network/config/" + NET_ID + "/shutdown", jh, method="POST")
    print("    shutdown st=%s body=%s" % (st, json.dumps(b, ensure_ascii=False)[:150]))
    time.sleep(4)
    print("    shutdown 后:", gw_state())

    print("[4] start 网络组件 ...")
    st, b = req(JL + "/network/config/" + NET_ID + "/start", jh, method="POST")
    print("    start st=%s body=%s" % (st, json.dumps(b, ensure_ascii=False)[:150]))
    time.sleep(10)
    print("    start 后+10s:", gw_state())
    time.sleep(15)
    print("    start 后+25s:", gw_state())


if __name__ == "__main__":
    main()
