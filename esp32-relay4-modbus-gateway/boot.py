# boot.py — 空堆窗口内预连 WiFi，避免 main.py 加载挤占 esp_sha DMA buffer。
#
# 2026-09-09 实测根因：完整加载 main.py(~69KB) 后 GC split-heap 从
# ESP-IDF internal RAM 池扩容，WiFi 握手期的 esp_sha buffer 分配持续失败
# （"esp-sha: Failed to allocate buf memory"，~2.4s 一次 -> status 202），
# 应用层无论怎么重试都连不上。而空堆（仅 boot/wifi_boot 加载）下一次
# connect 即成功。故把 WiFi 连接提前到本文件：先连上，再让 MicroPython
# 自动加载 main.py（此时堆扩大不再影响已完成的握手；main.py 的 connect_wifi
# 发现 isconnected()=True 会直接跳过）。
#
# 掉线兜底：main.py 复位风暴守卫 machine.reset() -> 本文件空堆重连 -> 成功
# -> main 复跑。无 wifi 配置时 ensure() 立即返回，不影响首次 portal 配网。
import wifi_boot
wifi_boot.ensure(45)
