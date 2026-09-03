# -*- coding: utf-8 -*-
"""只读：修正 JetLinks 属性查询(带 sorts / paging)，打印 500 详情。"""
import json
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
        return -1, {"exc": str(e)[:200]}


def main():
    st, body = req(JL + "/authorize/login", headers={"Content-Type": "application/json"},
                   data={"username": "admin5", "password": "Admin@group5"})
    jtok = (body.get("result") or {}).get("token")
    jh = {"X-Access-Token": jtok, "Content-Type": "application/json"}

    dev = "MODBUS-TERM-01"
    cases = [
        ("分页+sorts", "/device/instance/%s/properties/_query" % dev,
         {"sorts": [{"name": "timestamp", "order": "desc"}],
          "paging": {"pageIndex": 0, "pageSize": 5}}),
        ("no-paging+sorts", "/device/instance/%s/properties/_query/no-paging" % dev,
         {"sorts": [{"name": "timestamp", "order": "desc"}]}),
        ("terms空+sorts空", "/device/instance/%s/properties/_query/no-paging" % dev,
         {"terms": [], "sorts": []}),
    ]
    for name, path, data in cases:
        st, b = req(JL + path, jh, data, method="POST")
        print("\n## %s -> %s" % (name, st))
        print(json.dumps(b, ensure_ascii=False)[:600])
        res = b.get("result") if isinstance(b, dict) else b
        if isinstance(res, dict):
            data_l = res.get("data")
            if isinstance(data_l, list) and data_l:
                print("  首条:", json.dumps(data_l[0], ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
