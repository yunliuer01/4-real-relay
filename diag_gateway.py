# -*- coding: utf-8 -*-
"""只读诊断：网关订阅主题 / 终端在线 / EMQX 规则 / JetLinks 设备绑定
不修改任何平台配置。"""
import json
import base64
import urllib.request
import urllib.error

EMQX = "http://172.16.4.211:9183/api/v5"
JETLINKS = "http://172.16.4.211:9000/api"
EMQX_AUTH = ("group5", "Admin@group5")
JL_AUTH = ("admin5", "Admin@group5")
GATEWAY_CLIENT = "jetlinks-g5-lfx"
GW_NET_ID = "2095085287379030016"
GATEWAY_ID = "2095085346338361344"
PRODUCT_ID = "mqtt-iot"
DEVICES = ["FILE-TERM-01", "MODBUS-TERM-01"]


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


def emqx_headers(token=None):
    if token:
        return {"Authorization": "Bearer " + token}
    b = base64.b64encode(f"{EMQX_AUTH[0]}:{EMQX_AUTH[1]}".encode()).decode()
    return {"Authorization": "Basic " + b}


def jl_headers(token):
    return {"X-Access-Token": token, "Content-Type": "application/json"}


def main():
    print("=" * 66)
    print("1) EMQX: 登录 Dashboard")
    st, body = http_req(EMQX + "/login", headers={"Content-Type": "application/json"},
                        data={"username": EMQX_AUTH[0], "password": EMQX_AUTH[1]})
    token = None
    if st == 200 and isinstance(body, dict):
        token = body.get("token") or (body.get("data") or {}).get("token")
    print("   status=%s token=%s" % (st, "YES" if token else "NO"))
    eh = emqx_headers(token)

    print("-" * 66)
    print("2) EMQX: 网关客户端 %s 状态与订阅" % GATEWAY_CLIENT)
    st, body = http_req(EMQX + "/clients/" + GATEWAY_CLIENT, headers=eh)
    if st == 200:
        c = body
        print("   connected=%s  ip=%s" % (c.get("connected"), c.get("ip_address")))
        print("   stats=%s" % json.dumps(c.get("stats", {}), ensure_ascii=False))
        print("   subscriptions_count=%s" % c.get("subscriptions_count"))
    else:
        print("   HTTP %s %s" % (st, json.dumps(body, ensure_ascii=False)[:300]))

    st, body = http_req(EMQX + "/clients/" + GATEWAY_CLIENT + "/subscriptions", headers=eh)
    if st == 200:
        subs = body if isinstance(body, list) else body.get("data", [])
        if not subs:
            print("   (无订阅)")
        for s in subs:
            print("   topic=%s  qos=%s" % (s.get("topic"), s.get("qos")))
    else:
        print("   subscriptions HTTP %s %s" % (st, json.dumps(body, ensure_ascii=False)[:300]))

    print("-" * 66)
    print("3) EMQX: 终端/网关在线客户端 (模糊匹配)")
    st, body = http_req(EMQX + "/clients?limit=100", headers=eh)
    if st == 200:
        clients = body.get("data", []) if isinstance(body, dict) else body
        for c in clients:
            cid = c.get("clientid", "")
            if any(k in cid for k in ("FILE-TERM", "MODBUS-TERM", "jetlinks-g5", "lfx")):
                print("   clientid=%-20s connected=%s  stats=%s" %
                      (cid, c.get("connected"),
                       json.dumps({k: v for k, v in (c.get("stats") or {}).items()
                                   if k in ("recv_msg", "send_msg", "recv_pkt", "send_pkt")})))
    else:
        print("   HTTP %s" % st)

    print("-" * 66)
    print("4) EMQX: 规则引擎规则列表 (rule_lfx*)")
    st, body = http_req(EMQX + "/rules", headers=eh)
    if st == 200:
        rules = body if isinstance(body, list) else body.get("data", [])
        if not rules:
            print("   (无规则)")
        for r in rules:
            rid = r.get("id", "")
            if "lfx" in rid.lower() or "rule" in rid.lower():
                metrics = r.get("metrics", {})
                print("   id=%s enabled=%s  sql=%s" % (rid, r.get("enabled"), (r.get("sql") or "")[:80].replace("\n", " ")))
                print("      matched=%s  passed=%s  failed=%s  actions_total=%s  actions_failed=%s" % (
                    metrics.get("sql.matched"), metrics.get("sql.passed"),
                    metrics.get("sql.failed"), metrics.get("actions.total"),
                    metrics.get("actions.failed")))
    else:
        print("   HTTP %s %s" % (st, json.dumps(body, ensure_ascii=False)[:200]))

    print("-" * 66)
    print("5) JetLinks: 登录")
    st, body = http_req(JETLINKS + "/authorize/login",
                        headers={"Content-Type": "application/json"},
                        data={"username": JL_AUTH[0], "password": JL_AUTH[1]})
    token = None
    if st == 200:
        token = (body.get("result") or {}).get("token")
    print("   status=%s token=%s" % (st, "YES" if token else "NO"))
    if not token:
        print("   JetLinks 登录失败，跳过设备检查")
        return
    jh = jl_headers(token)

    st, body = http_req(JETLINKS + "/device/instance/_query/no-paging",
                        headers=jh, method="POST",
                        data={"paging": False,
                              "terms": [{"column": "id$deviceId", "value": DEVICES}]})
    if st != 200:
        st, body = http_req(JETLINKS + "/device/instance/_query/no-paging",
                            headers=jh, method="POST",
                            data={"paging": False, "terms": []})
    if st == 200:
        items = body.get("result", []) if isinstance(body, dict) else body
        if not isinstance(items, list):
            items = []
        for d in items:
            if d.get("id") in DEVICES or d.get("name", "").find("温湿度") >= 0:
                print("   id=%-16s state=%-7s product=%s name=%s" % (
                    d.get("id"), (d.get("state") or {}).get("value"),
                    d.get("productId"), d.get("name")))
    else:
        print("   HTTP %s %s" % (st, json.dumps(body, ensure_ascii=False)[:200]))

    print("-" * 66)
    print("6) JetLinks: 产品 %s 接入配置" % PRODUCT_ID)
    st, body = http_req(JETLINKS + "/device/product/_query/no-paging",
                        headers=jh, method="POST",
                        data={"paging": False,
                              "terms": [{"column": "id", "value": PRODUCT_ID}]})
    if st == 200:
        items = body.get("result", []) if isinstance(body, dict) else body
        if isinstance(items, list) and items:
            p = items[0]
            print("   id=%s name=%s" % (p.get("id"), p.get("name")))
            print("   accessId=%s accessName=%s accessProvider=%s" % (
                p.get("accessId"), p.get("accessName"), p.get("accessProvider")))
            print("   storePolicy=%s" % p.get("storePolicy"))
            # 物模型里的属性/功能
            md = p.get("metadata") or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = {}
            props = [e.get("id") for e in (md.get("properties") or [])]
            funcs = [e.get("id") for e in (md.get("functions") or [])]
            print("   properties=%s functions=%s" % (props, funcs))
        else:
            print("   result=%s" % (json.dumps(body, ensure_ascii=False)[:200]))
    else:
        print("   HTTP %s %s" % (st, json.dumps(body, ensure_ascii=False)[:200]))

    print("=" * 66)


if __name__ == "__main__":
    main()
