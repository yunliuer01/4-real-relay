# -*- coding: utf-8 -*-
"""只读+一条无害探测：手工发布一条与真实值一致的消息到网关订阅主题，
验证 jetlinks-g5-lfx 是否真的能收到投递。"""
import json
import time
import urllib.request
import urllib.error
import paho.mqtt.client as mqtt

EMQX = "http://172.16.4.211:9183/api/v5"


def http_req(url, headers=None, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def gw_stats(eh):
    st, b = http_req(EMQX + "/clients/jetlinks-g5-lfx", headers=eh)
    if st != 200:
        return {"err": st}
    return {
        "connected_at": b.get("connected_at"),
        "recv_cnt": b.get("recv_cnt"),
        "recv_msg": b.get("recv_msg"),
        "recv_oct": b.get("recv_oct"),
        "send_cnt": b.get("send_cnt"),
        "send_msg": b.get("send_msg"),
    }


def main():
    st, body = http_req(EMQX + "/login", headers={"Content-Type": "application/json"},
                        data={"username": "group5", "password": "Admin@group5"})
    tok = body.get("token") or (body.get("data") or {}).get("token")
    eh = {"Authorization": "Bearer " + (tok or "")}

    print("发布前:", gw_stats(eh))
    c = mqtt.Client(client_id="diag_probe_gw5", protocol=mqtt.MQTTv311)
    c.username_pw_set("test", "123456")
    c.connect("172.16.4.211", 9783, 30)
    payload = json.dumps({"deviceId": "FILE-TERM-01", "properties": {"temperature": 28.2, "humidity": 59.1}})
    info = c.publish("/mqtt-iot/FILE-TERM-01/properties/report", payload, qos=1)
    print("发布返回 rc=%s mid=%s" % (info.rc, info.mid))
    time.sleep(3)   # 给投递留时间
    c.disconnect()
    print("发布后+3s:", gw_stats(eh))
    time.sleep(6)
    print("发布后+9s:", gw_stats(eh))


if __name__ == "__main__":
    main()
