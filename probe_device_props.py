# -*- coding: utf-8 -*-
"""只读探测：JetLinks 设备最新属性/详情，判定平台是否真的在消费两终端数据。"""
import json
import time
import urllib.request
import urllib.error

JL = "http://172.16.4.211:9000/api"


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
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    jtok = (body.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}
    print("login:", st)

    for dev in ("FILE-TERM-01", "MODBUS-TERM-01"):
        print("\n" + "=" * 60)
        print("设备:", dev)
        # 候选1: 设备详情
        st, b = req(JL + "/device/instance/" + dev + "/detail", jh)
        if st == 200:
            d = b.get("result") if isinstance(b, dict) else b
            if isinstance(d, dict):
                props = d.get("properties")
                latest = d.get("latest") or {}
                print("  detail 200: state=%s online=%s props型=%s" %
                      (json.dumps(d.get("state"), ensure_ascii=False), d.get("online"),
                       type(props).__name__))
                if isinstance(props, dict) and props:
                    print("  props keys:", list(props.keys())[:10])
                    print("  props:", json.dumps(props, ensure_ascii=False)[:300])
                if isinstance(latest, dict) and latest:
                    print("  latest:", json.dumps(latest, ensure_ascii=False)[:300])
        else:
            print("  detail ->", st)
        time.sleep(0.4)
        # 候选2: 属性存储查询(最新一条)
        for path in ("/device/instance/%s/properties/_query/no-paging",
                     "/device/%s/properties/_query/no-paging"):
            p = path % dev
            st, b = req(JL + p, jh, {"paging": False}, method="POST")
            if st == 200:
                res = b.get("result") if isinstance(b, dict) else b
                if isinstance(res, list) and res:
                    print("  %s -> 200 共%s条 首条: %s" %
                          (p, len(res), json.dumps(res[0], ensure_ascii=False)[:220]))
                    if len(res) > 1:
                        print("     末条: %s" % json.dumps(res[-1], ensure_ascii=False)[:220])
                else:
                    print("  %s -> 200 raw: %s" % (p, json.dumps(b, ensure_ascii=False)[:200]))
            else:
                print("  %s -> %s" % (p, st))
            time.sleep(0.4)


if __name__ == "__main__":
    main()
