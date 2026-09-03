# -*- coding: utf-8 -*-
"""重启网络组件后诊断：终端进程/连接 + 网关订阅 + 手工 online 报文测试平台消费。只读 + 一条无害 online。"""
import json
import time
import urllib.request
import urllib.error

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"
GW = "jetlinks-g5-lfx"


def req(url, headers=None, data=None, method=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
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
    # ---- JetLinks 登录 ----
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    jtok = (body.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}
    print("[JL] login:", st)

    # ---- EMQX 登录 ----
    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": "group5", "password": "Admin@group5"})
    etok = body.get("token") or (body.get("data") or {}).get("token")
    eh = {"Authorization": "Bearer " + (etok or "")}

    def dev_state(dev):
        st, d = req(JL + "/device/instance/_query/no-paging", jh,
                    {"terms": [{"column": "id", "termType": "eq", "value": dev}]})
        res = d.get("result") if isinstance(d, dict) else d
        if isinstance(res, list) and res:
            return (res[0].get("state") or {}).get("value")
        return "err%d" % st

    # ---- 1. 终端连接计数（确认还在发）----
    print("\n=== 1. 终端连接计数（两次采样 10s）===")
    st, allc = req(EMQX + "/clients?limit=200", eh)
    cl = allc.get("data") if isinstance(allc, dict) else allc
    mine = [c for c in (cl or []) if "TERM" in (c.get("clientid") or "").upper() or c.get("clientid") == GW]
    if not mine:
        # 尝试遍历
        for c in (cl or []):
            cid = c.get("clientid") or ""
            if any(k in cid for k in ("TERM", GW, "terminal")):
                mine.append(c)

    def snapshot():
        out = {}
        st, allc = req(EMQX + "/clients?limit=500", eh)
        cl = allc.get("data") if isinstance(allc, dict) else allc
        for c in (cl or []):
            cid = c.get("clientid") or ""
            if any(k in cid for k in ("TERM", "terminal")) and "diag" not in cid:
                out[cid] = (c.get("recv_msg"), c.get("connected_at"))
        st, b = req(EMQX + "/clients/" + GW, eh)
        if st == 200:
            out["==GW=="] = ("send_msg=%s" % b.get("send_msg"),
                             b.get("connected_at") + " subs待查")
        return out

    s1 = snapshot()
    time.sleep(10)
    s2 = snapshot()
    for cid in s2:
        v1 = s1.get(cid)
        v2 = s2[cid]
        d = ""
        if v1 and v2 and isinstance(v1[0], int) and isinstance(v2[0], int):
            d = "+%d/10s" % (v2[0] - v1[0])
        print("  %-30s %s%s" % (cid, v2[0], d))

    # ---- 2. 网关订阅 ----
    print("\n=== 2. 网关订阅 ===")
    st, subs = req(EMQX + "/clients/" + GW + "/subscriptions", eh)
    sd = subs.get("data") if isinstance(subs, dict) else subs
    if isinstance(sd, list):
        print("  订阅数:", len(sd))
        for s in sd[:16]:
            print("   ", s.get("topic"), "qos", s.get("qos"))
    else:
        print("  订阅查询 st=%s raw=%s" % (st, json.dumps(subs, ensure_ascii=False)[:200]))

    # ---- 3. 手工 online 报文测试平台消费 ----
    print("\n=== 3. 手工 online 报文测试 ===")
    print("  发布前 FILE state:", dev_state("FILE-TERM-01"))
    import paho.mqtt.client as mqtt
    c = mqtt.Client(client_id="diag_online_post", protocol=mqtt.MQTTv311)
    c.username_pw_set("test", "123456")
    c.connect("172.16.4.211", 9783, 30)
    c.publish("/mqtt-iot/FILE-TERM-01/online", json.dumps({"deviceId": "FILE-TERM-01"}), qos=1)
    time.sleep(1)
    c.disconnect()
    print("  已发布 online 报文")
    time.sleep(8)
    print("  发布后 FILE state:", dev_state("FILE-TERM-01"))
    time.sleep(8)
    print("  +16s   FILE state:", dev_state("FILE-TERM-01"))

    # ---- 4. 网关投递计数最终采样 ----
    st, b = req(EMQX + "/clients/" + GW, eh)
    if st == 200:
        print("\n=== 4. 网关最终状态 ===")
        print("  connected_at=%s send_msg=%s send_oct=%s" % (b.get("connected_at"), b.get("send_msg"), b.get("send_oct")))


if __name__ == "__main__":
    main()
