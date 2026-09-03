# -*- coding: utf-8 -*-
"""只读端到端验证：网关投递计数 + JetLinks 设备在线状态/最新属性。"""
import json
import time
import urllib.request
import urllib.error

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"
GW = "jetlinks-g5-lfx"


def req(url, headers=None, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"exc": str(e)[:80]}


def main():
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    jtok = (body.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}
    print("[JL] login:", st)

    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": "group5", "password": "Admin@group5"})
    etok = body.get("token") or (body.get("data") or {}).get("token")
    eh = {"Authorization": "Bearer " + (etok or "")}

    def gw():
        st, b = req(EMQX + "/clients/" + GW, eh)
        if st == 200:
            return "connected_at=%s send_msg=%s send_oct=%s recv_cnt=%s" % (
                b.get("connected_at"), b.get("send_msg"), b.get("send_oct"), b.get("recv_cnt"))
        return "st=%s" % st

    print("[网关] t0:", gw())
    time.sleep(20)
    print("[网关] t+20s:", gw())

    # JetLinks 设备状态查询
    for dev in ("FILE-TERM-01", "MODBUS-TERM-01"):
        st, d = req(JL + "/device/instance/_query/no-paging", jh, {
            "terms": [{"column": "id", "termType": "eq", "value": dev}]})
        res = d.get("result") if isinstance(d, dict) else d
        if isinstance(res, list) and res:
            r = res[0]
            print("[设备] %s state=%s online=%s" % (dev, json.dumps(r.get("state"), ensure_ascii=False), r.get("online")))
        else:
            print("[设备] %s 查询 st=%s raw=%s" % (dev, st, json.dumps(d, ensure_ascii=False)[:160]))
        time.sleep(1)


if __name__ == "__main__":
    main()
