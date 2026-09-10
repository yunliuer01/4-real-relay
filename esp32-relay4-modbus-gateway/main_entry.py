# -*- coding: utf-8 -*-
"""板载 main.py（引导壳）—— 真正的固件逻辑在 app_main.mpy。

为什么需要这个壳（2026-09-10 定案）：
  ESP32-C3 上 boot.py 预连 WiFi 后可用堆 ~158KB，而 pyexec_file 直接编译源码的
  峰值 ≈ 2x 源码（源码字符串 + 解析树 + 字节码要同时驻留）—— 源码 >80KB 就
  MemoryError 起不来（v6.0.4 的 79857 B 是勉强过的，v6.0.5 的 97457 B 直接崩）。
  改用 mpy-cross 预编译字节码后：app_main.mpy 仅 27777 B（源码 28.5%），
  实板加载成本 39.8KB，加载后剩余堆 118KB，可增长空间从 80KB 抬到 ~290KB。

部署方式：
  main_entry.py --(改名)--> 板子 /main.py
  main.py       --(build_mpy.py)--> 板子 /app_main.mpy
详见 .workbuddy/memory/ROOTCAUSES.md 第 10 条。
"""

import gc

gc.collect()  # 收一次，尽量给 .mpy 反序列化留连续块

try:
    import app_main
except ImportError as _e:
    print("[entry] app_main.mpy 加载失败:", _e)
    print("[entry] 请先跑 build_mpy.py 生成并上传 app_main.mpy")
    raise
