# -*- coding: utf-8 -*-
"""抓包对比 FILE/MODBUS 消息 + 采样两个网关投递增量 + 设备在线状态。只读。"""
import json
import time
import urllib.request
import urllib.error
import threading
import paho.mqtt.client as mqtt

JL = "http://172.16.4.211:9000/api"
EMQX = "http://172.16.4.211:9183/api/v5"


def req(url, headers=None, data=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
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


st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
               data={"username": "admin5", "password": "Admin@group5"})
jtok = (body.get("result") or {}).get("token")
jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}

st, body = req(EMQX + "/login", headers={"Content-Type": "application/json"},
               data={"username": "group5", "password": "Admin@group5"})
etok = body.get("token") or (body.get("data") or {}).get("token")
eh = {"Authorization": "Bearer " + (etok or "")}


def dev_full(dev):
    st, d = req(JL + "/device/instance/_query/no-paging", jh,
                {"terms": [{"column": "id", "termType": "eq", "value": dev}]})
    res = d.get("result") if isinstance(d, dict) else d
    if isinstance(res, list) and res:
        r = res[0]
        return {
            "state": (r.get("state") or {}).get("value"),
            "online": r.get("online"),
            "offlineTime": r.get("offlineTime"),
            "lastTime": r.get("lastTime"),
            "registryTime": r.get("registryTime"),
        }
    return {"err": st}


def gw_send(cid):
    st, b = req(EMQX + "/clients/" + cid, eh)
    return (b or {}).get("send_msg") if st == 200 else None


msgs = []
lock = threading.Lock()


def on_msg(client, userdata, msg):
    try:
        p = json.loads(msg.payload.decode())
        brief = {k: p[k] for k in ("deviceId", "device_id", "deviceName") if k in p}
        props = p.get("properties") or p.get("data") or {}
        if isinstance(props, dict):
            brief["props"] = {k: props[k] for k in list(props)[:4]}
    except Exception:
        brief = {"raw": msg.payload[:120]}
    with lock:
        msgs.append((time.strftime("%H:%M:%S"), msg.topic, brief))


c = mqtt.Client(client_id="diag_cap_all", protocol=mqtt.MQTTv311)
c.username_pw_set("test", "123456")
c.on_message = on_msg
c.connect("172.16.4.211", 9783, 30)
c.loop_start()
time.sleep(1)
c.subscribe("/mqtt-iot/#", qos=0)
time.sleep(0.5)

g5a, eixa = gw_send("jetlinks-g5-lfx"), gw_send("jetlinks-emqx")
print("开始抓包 25s... (g5 send_msg=%s, jetlinks-emqx send_msg=%s)" % (g5a, eixa))
time.sleep(25)
g5b, eixb = gw_send("jetlinks-g5-lfx"), gw_send("jetlinks-emqx")
c.loop_stop()

print("\n=== 抓到的消息 (%d 条) ===" % len(msgs))
for t, topic, b in msgs:
    print("  %s %-65s %s" % (t, topic, json.dumps(b, ensure_ascii=False)[:140]))

print("\n=== 网关 25s 增量 ===")
print("  jetlinks-g5-lfx send_msg: %s -> %s (Δ%s)" % (g5a, g5b, (g5b or 0) - (g5a or 0)))
print("  jetlinks-emqx  send_msg: %s -> %s (Δ%s)" % (eixa, eixb, (eixb or 0) - (eixa or 0)))

print("\n=== 设备状态 ===")
for dev in ("FILE-TERM-01", "MODBUS-TERM-01"):
    print("  %s: %s" % (dev, json.dumps(dev_full(dev), ensure_ascii=False)))
