# -*- coding: utf-8 -*-
"""JetLinks 资源升级：把产品 relay8_lfx 物模型升级为「8路继电器完整版」

在 create_product.py（旧模型 r1..r8 + write）之上，把物模型升级为与
demo relay_8ch_product 一致（见 setup/relay8_thing_model.py）：
  - 34 属性（每路 状态/电压/电流/功率 + 环境温度/湿度）
  - 1 事件 switch_change
  - 2 功能 set_channel / switch_all
仅更新物模型，其它字段(接入、协议、分类、名称等)保持原值；不动别人/默认配置。
设备侧物模型快照由 JetLinks 随产品自动同步。

幂等：重复运行安全（PUT 原位更新）。设备升级后重启 core/simulator.py 即生效。
运行：python setup/upgrade_product_model.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay8_thing_model import build_metadata, metadata_summary  # noqa: E402

BASE = config.JETLINKS_API
PRODUCT_ID = config.PRODUCT_ID


def req(method, path, data=None, token=None, timeout=30):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("X-Access-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return -1, {"exc": str(e)}


def main():
    _, r = req("POST", "/authorize/login",
               {"username": config.JETLINKS_WEB_USER,
                "password": config.JETLINKS_WEB_PASS})
    token = r["result"]["token"]
    print("登录 OK")

    st, r = req("GET", f"/device/product/{PRODUCT_ID}", token=token)
    if st != 200:
        print(f"读取产品失败 code={st}: {json.dumps(r, ensure_ascii=False)[:300]}")
        return
    p = r.get("result") or {}
    print(f"当前产品: {p.get('id')} name={p.get('name')} "
          f"metadata={len(p.get('metadata') or '')}字符")

    new_meta = build_metadata()
    n_p, n_e, n_f = metadata_summary(new_meta)
    print(f"新物模型: 属性={n_p} 事件={n_e} 功能={n_f}")

    upd = {k: v for k, v in p.items()
           if k not in ("createTime", "creatorId", "creatorName",
                        "modifyTime", "modifierId", "modifierName")}
    if isinstance(upd.get("deviceType"), dict):  # 后端接收字符串
        upd["deviceType"] = upd["deviceType"].get("value")
    upd["metadata"] = new_meta

    st, r = req("PUT", f"/device/product/{PRODUCT_ID}", data=upd, token=token)
    print(f"PUT /device/product/{PRODUCT_ID} -> {st}")
    if st not in (200, 201):
        print(json.dumps(r, ensure_ascii=False)[:500])
        st, r = req("PUT", f"/product/{PRODUCT_ID}", data=upd, token=token)
        print(f"备用路径 PUT /product/{PRODUCT_ID} -> {st}")
        print(json.dumps(r, ensure_ascii=False)[:500])
        if st not in (200, 201):
            return

    st, r = req("GET", f"/device/product/{PRODUCT_ID}", token=token)
    if st == 200:
        md = (r.get("result") or {}).get("metadata") or ""
        cp, ce, cf = metadata_summary(md)
        print(f"升级后确认: properties={cp} events={ce} functions={cf}")
        ids = [x["id"] for x in json.loads(md).get("properties", [])]
        print("属性样例:", ids[:4], "...", ids[-2:])
    else:
        print("校验读取失败", st)


if __name__ == "__main__":
    main()
