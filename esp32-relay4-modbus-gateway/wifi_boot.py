# -*- coding: utf-8 -*-
"""wifi_boot.py — boot 阶段空堆预连 WiFi。

背景（2026-09-09 实测）：
  完整加载 main.py(~69KB) 后 MicroPython split-heap 自动从 ESP-IDF internal
  RAM 池扩容，占满 WiFi 驱动 esp_sha DMA buffer 所需内存 -> 握手期每个
  buffer 分配都失败（"esp-sha: Failed to allocate buf memory"，~2.4s 一次），
  status 落 202，永远连不上。REPL 空堆对比：一次 connect 即成功。
  结论：WiFi 握手必须在堆还空的时候完成 -> 放到 boot.py（本模块）先跑，
  连上后 MicroPython 再加载 main.py，此时堆扩大已伤不到已完成的握手。

  掉线兜底：main.py 复位风暴守卫 machine.reset() -> boot.py 空堆重连 ->
  成功 -> main 复跑，架构自洽。

本模块刻意保持小体积（不 import main），只做一件事：连 WiFi。
"""
import json
import network
import time
import gc

IP = ""  # 连接成功后填充，便于 main 侧查询（非必需，驱动状态已共享）


def ensure(timeout_s=45):
    """读 config.json 并连接 WiFi；成功返回 True，无配置/失败返回 False。"""
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
    except Exception as e:
        print("[wifi-boot] no config.json:", e)
        return False
    ssid = cfg.get("wifi_ssid") or ""
    pwd = cfg.get("wifi_password") or ""
    if not ssid:
        print("[wifi-boot] no wifi_ssid in config, skip (main will portal)")
        return False
    gc.collect()
    print("[wifi-boot] heap_free=%d before connect" % gc.mem_free())
    w = network.WLAN(network.STA_IF)
    w.active(True)
    time.sleep_ms(1200)  # 等驱动就绪
    t0 = time.ticks_ms()
    tries = 0
    while not w.isconnected() and \
            time.ticks_diff(time.ticks_ms(), t0) < timeout_s * 1000:
        st = w.status()
        if st == 1000:
            # 驱动空闲 -> 发连接
            tries += 1
            print("[wifi-boot] connect issued try#%d ssid=%s"
                  % (tries, ssid))
            w.connect(ssid, pwd)
        elif st in (201, 202, 203, 1002, 1003, 1004):
            # 失败码（esp-sha 瞬时/认证/无AP 等）-> 断开等驱动回 IDLE 重发
            print("[wifi-boot] fail code %d -> disconnect, wait 2s" % st)
            try:
                w.disconnect()
            except Exception:
                pass
            time.sleep(2)
        # 短轮询等待结果
        for _ in range(4):
            if w.isconnected():
                break
            time.sleep(0.25)
    if w.isconnected():
        IP = w.ifconfig()[0]
        print("[wifi-boot] CONNECTED ip=%s heap_free=%d"
              % (IP, gc.mem_free()))
        return True
    print("[wifi-boot] FAILED after %ds (main will retry/portal)" % timeout_s)
    return False
