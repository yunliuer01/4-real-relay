# boot.py — 空堆窗口内预连 WiFi + 交棒前 GC，规避 main.py 加载时的堆碎片。
#
# 2026-09-09 实测根因：完整加载 main.py 后 GC split-heap 从 ESP-IDF internal RAM
# 池扩容，WiFi 握手期的 esp-sha buffer 分配持续失败（"esp-sha: Failed to allocate
# buf memory"，~2.4s 一次 -> status 202），应用层怎么重试都连不上。而空堆（仅
# boot/wifi_boot 加载）下一次 connect 即成功。故把 WiFi 连接提前到本文件：先连上，
# 再让 MicroPython 自动加载 main.py（main.py 的 connect_wifi 见 isconnected()
# 为 True 会直接跳过）。
#
# 掉线兜底：main.py 复位风暴守卫 machine.reset() -> 本文件空堆重连 -> main 复跑。
# 无 wifi 配置时 ensure() 立即返回，不影响首次 portal 配网。
import gc

import wifi_boot

wifi_boot.ensure(45)

# 2026-09-10：交棒前做两个清理，给 main.py 的编译腾出连续空闲块。
# 现象：main.py 能跑到 [MAIN] 之前就 MemoryError，且报的是 **1768 字节**这种
# 很小的请求 —— 说明总空闲够、但被碎片切碎了拿不到连续块（同一份文件有时能起
# 有时不能起）。删掉 wifi_boot 模块释放其函数/代码对象，再 gc.collect() 让
# MicroPython 把相邻空闲块合并。详见 ROOTCAUSES.md 第 10 条。
del wifi_boot
gc.collect()
