# -*- coding: utf-8 -*-
"""平台下行链路测试：JetLinks REST 触发功能调用，观察下行主题与终端回复
只读观察 + 触发一次功能调用，不改任何配置。
"""
import json
import threading
import time
import urllib.request
import urllib.error

import paho.mqtt.client as mqtt

EMQX_HOST = "172.16.4.211"
EMQX_PORT = 9783
DASH = "http://172.16.4.211:9183/api/v5"
JL = "http://172.16.4.211:9000/api"
DEV = "FILE-TERM-01"

msgs = []
lock = threading.Lock()


def jl_req(method, path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(JL + path, data=body,
                               headers={"Content-Type": "application/json"})
    r.get_method = lambda: method
    if token:
        r.add_header("X-Access-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return json.loads(x.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_msg": e.read(300).decode("utf-8", "replace")}


def main():
    tok = jl_req("POST", "/authorize/login",
                 {"username": "admin5", "password": "Admin@group5"})["result"]["token"]

    # 终端客户端 IP 确认
    dash_tok = json.load(urllib.request.urlopen(
        urllib.request.Request(DASH + "/login",
                               data=json.dumps({"username": "group5", "password": "Admin@group5"}).encode(),
                               headers={"Content-Type": "application/json"}), timeout=15))["token"]
    for cid in ["FILE-TERM-01", "FILE-TERM-01-9554a2", "MODBUS-TERM-01-a79564", "jetlinks-g5-lfx"]:
        try:
            r = urllib.request.Request(DASH + "/clients/" + cid,
                                       headers={"Authorization": "Bearer " + dash_tok})
            with urllib.request.urlopen(r, timeout=10) as x:
                d = json.loads(x.read().decode())
            print(f"客户端 {cid}: ip={d.get('ip_address')} connected={d.get('connected')}")
        except Exception as e:
            print(f"客户端 {cid}: 查询失败 {e}")

    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id="dlink-probe")
    c.username_pw_set("test", "123456")

    def on_msg(cl, ud, m):
        with lock:
            msgs.append((time.time(), m.topic, m.payload.decode("utf-8", "replace")))
    c.on_message = on_msg
    c.connect(EMQX_HOST, EMQX_PORT, 60)
    c.loop_start()
    c.subscribe("/mqtt-iot/#", qos=1)
    time.sleep(1)

    # 触发平台功能调用 setInterval=9
    print("\n>> 触发 JetLinks 功能调用: setInterval(9)")
    resp = jl_req("POST", f"/device/instance/{DEV}/function/setInterval",
                  {"params": {"interval": 9}}, token=tok)
    print("   REST 响应:", json.dumps(resp, ensure_ascii=False)[:300])

    print("\n>> 监听 15s 内的消息 ...")
    time.sleep(15)
    with lock:
        snapshot = list(msgs)
    if not snapshot:
        print("   (15s 内未捕获到任何 /mqtt-iot/# 消息)")
    for ts, t, p in snapshot:
        print(f"   [{time.strftime('%H:%M:%S', time.localtime(ts))}] {t}\n       {p[:200]}")
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
