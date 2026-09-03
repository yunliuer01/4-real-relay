# -*- coding: utf-8 -*-
"""只读现场检查（第2步）：网关运行状态 / 投递增量 / 两设备状态 / 谁在消费 mqtt-iot 主题。
不修改任何平台配置。"""
import json
import time
import urllib.request
import urllib.error

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"
GW = "jetlinks-g5-lfx"
GW_NET_ID = "2095085287379030016"      # 网络组件 g5_mqtt_client
GATEWAY_ID = "2095085346338361344"     # 设备接入网关 g5-mqtt-gateway
PRODUCT_ID = "mqtt-iot"
DEVICES = ["FILE-TERM-01", "MODBUS-TERM-01"]


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
    print("=" * 70)
    # ---- JetLinks 登录 ----
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    jtok = (body.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}
    print("[1] JetLinks login:", st)

    # ---- EMQX 登录 ----
    st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
                   data={"username": "group5", "password": "Admin@group5"})
    etok = body.get("token") or (body.get("data") or {}).get("token")
    eh = {"Authorization": "Bearer " + (etok or "")}
    print("[1] EMQX login:", st)

    # ---- 2. JetLinks: 设备接入网关真实状态（试几个只读端点）----
    print("\n[2] 设备接入网关 g5-mqtt-gateway 状态探测:")
    for path in ("/device/gateway/" + GATEWAY_ID,
                 "/device/gateway/_query/no-paging",
                 "/gateway/device/" + GATEWAY_ID,
                 "/gateway/_query/no-paging"):
        if path.endswith("no-paging"):
            st, b = req(JL + path, jh, {"paging": False,
                                        "terms": [{"column": "id", "termType": "eq", "value": GATEWAY_ID}]},
                        method="POST")
        else:
            st, b = req(JL + path, jh)
        txt = json.dumps(b, ensure_ascii=False)
        if st == 200:
            item = None
            if isinstance(b, dict):
                item = b.get("result") if isinstance(b.get("result"), list) and b.get("result") else (
                    b if b.get("id") else None)
            if isinstance(item, list) and item:
                item = item[0]
            if isinstance(item, dict):
                s = item.get("state") or {}
                print("   %-34s -> 200 id=%s name=%s state=%s" %
                      (path, item.get("id"), item.get("name"), s.get("value")))
            else:
                print("   %-34s -> 200 %s" % (path, txt[:200]))
        else:
            print("   %-34s -> %s" % (path, st))
        time.sleep(0.4)

    # ---- 3. 网关客户端计数：两次采样 15s ----
    print("\n[3] 网关 %s 投递计数（15s 增量）:" % GW)

    def gw():
        st, b = req(EMQX + "/clients/" + GW, eh)
        if st == 200:
            return (b.get("send_msg"), b.get("recv_msg"),
                    b.get("send_cnt"), b.get("recv_cnt"),
                    b.get("connected_at"))
        return None

    def dev_state(dev):
        st, d = req(JL + "/device/instance/_query/no-paging", jh,
                    {"terms": [{"column": "id", "termType": "eq", "value": dev}]})
        res = d.get("result") if isinstance(d, dict) else d
        if isinstance(res, list) and res:
            r = res[0]
            return "%s/%s" % ((r.get("state") or {}).get("value"), r.get("online"))
        return "err%d" % st

    s1 = gw()
    print("   t0  :", s1)
    time.sleep(15)
    s2 = gw()
    print("   t+15:", s2)
    if s1 and s2:
        print("   => send_msg 增量=%s  recv_msg 增量=%s" % (s2[0] - s1[0], s2[1] - s1[1]))

    # ---- 4. 谁在订阅/连接 mqtt-iot 相关 ----
    print("\n[4] EMQX 上与本项目相关的在线客户端及其订阅:")
    st, allc = req(EMQX + "/clients?limit=500", eh)
    cl = allc.get("data") if isinstance(allc, dict) else allc
    for c in (cl or []):
        cid = c.get("clientid") or ""
        if any(k in cid for k in ("TERM", "jetlinks", "lfx", "gateway", "mqtt-iot")) and "diag" not in cid:
            sub = "?"
            st2, sb = req(EMQX + "/clients/" + cid + "/subscriptions", eh)
            sd = sb.get("data") if isinstance(sb, dict) else sb
            if isinstance(sd, list):
                sub = ",".join("%s(q%s)" % (x.get("topic"), x.get("qos")) for x in sd[:6])
            print("   %-22s recv=%s send=%s 订阅: %s" %
                  (cid, c.get("recv_msg"), c.get("send_msg"), sub))

    # ---- 5. 两设备在 JetLinks 的在线状态 ----
    print("\n[5] JetLinks 设备状态:")
    for dev in DEVICES:
        print("   %s -> state/online = %s" % (dev, dev_state(dev)))
        time.sleep(0.5)

    # ---- 6. EMQX 规则指标 ----
    print("\n[6] EMQX 规则引擎指标(规则 id 含 lfx):")
    st, rb = req(EMQX + "/rules", eh)
    rules = rb if isinstance(rb, list) else rb.get("data", [])
    for r in rules or []:
        rid = r.get("id", "")
        if "lfx" in rid.lower():
            m = r.get("metrics") or {}
            print("   %s enabled=%s matched=%s passed=%s failed=%s act_total=%s act_failed=%s" % (
                rid, r.get("enabled"), m.get("sql.matched"), m.get("sql.passed"),
                m.get("sql.failed"), m.get("actions.total"), m.get("actions.failed")))
    print("=" * 70)


if __name__ == "__main__":
    main()
