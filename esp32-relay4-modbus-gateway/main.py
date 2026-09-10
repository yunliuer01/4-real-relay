# -*- coding: utf-8 -*-
"""4 路继电器 + Modbus TCP/RTU 采集网关 v3.0 (MicroPython / ESP32-C3)

功能：
- 配网模式：长按 SW1 5 秒进入 AP 热点，IP 192.168.4.1，Web 配置 MQTT/产品/设备/Modbus 参数
- 正常模式：连 WiFi → 连 MQTT → 按 JetLinks 协议上报属性/事件/响应命令
- Modbus TCP 主站：独立线程轮询多从站（host/port/unit_id）、多寄存器，不阻塞继电器控制
- 持久化：配置写入 flash /config.json，掉电不丢失
- 断线重连：WiFi/MQTT 均支持自动重连
- HTTP 控制 API：STA 模式下端口 80，可直连控制继电器 / 模拟 SW1 动作 / 读写 Modbus 寄存器

硬件接线（CORE-ESP32-C3 四路继电器板）：
- 继电器低电平吸合：RELAY1=IO3, RELAY2=IO4, RELAY3=IO5, RELAY4=IO7
- SW1 按键：IO10，上拉输入，按下为低电平（长按 5 秒进配网）
- SW 按键：IO9（板上 BOOT 按钮同脚；运行时为普通 GPIO 可作按钮用，注意 boot 期间按下会进 download mode）
- LED 指示灯：IO2
- 保留 Modbus RTU (UART1)：默认 TX=IO20, RX=IO21, RS485 方向控制=IO8（mode=rtu 时启用）
- Modbus TCP：通过 STA WiFi 走 socket，无需额外硬件接线

版本要点（近期）：
- v6.0.5：① HTTP 控制 API 改为主循环非阻塞轮询（修「/api/relay 并发>=4 整机静默
  冻结」）；② 自愈 D：WiFi 假死（isconnected() 真但数据面死）主动重新关联；
  ③ MQTT 建连前加带超时的 TCP 预检，避免阻塞 connect() 饿死主循环；
  ④ 自愈 D 冷启动冷却修正（wifi_reassoc_ms 初值 None）；⑤ 自愈 E：网络彻底
  不可用则 machine.reset() 回退到 boot 空堆预连；⑥ HTTP 有连接在途时轮询间隔
  压到 20ms（原固定 0.1s 把吞吐压到 ~5 连接/秒）。
  上述故障的完整根因/证据/排查手法见 .workbuddy/memory/ROOTCAUSES.md。
- v6.0.4：出站 publish 全部 QoS0 + 半死连接自愈 A/B/C（publish 连续失败 /
  网关心跳超时 / bridge 报告停滞 -> 主动断开重连）。
- v6.0.1：boot.py + wifi_boot.py 空堆预连 WiFi，规避 esp_sha DMA 内存挤占。

注意（v6.0.5 起关于本文件的体积约定已解除）：**本文件不再被板子直接编译**。
板上跑的是引导壳 `main_entry.py`（落为 /main.py，仅 1KB），本文件由
`build_mpy.py` 用 mpy-cross 预编译成 `app_main.mpy`（~28KB）再上传，
板上只 `import`，不驻留源码串与解析树 —— 之前「源码直编峰值 2x 撑爆堆」
（2026-09-10 MemoryError）由此彻底解决，体积余量从 ~80KB 抬到 ~290KB。
改完本文件**必须重跑 `build_mpy.py` 并重新部署**，否则板上跑的还是旧字节码
（`deploy.py` 有产物新鲜度守卫 + 仿真台 P8 会拦）。
"""
import json
import os
import network
import socket
import time
import machine
from machine import Pin, reset

# modbus 模块改为 start_modbus() 内按需 import：C3 堆紧张，两个模块同时
# import（源码合计 24KB，字节码常驻堆）会挤占线程栈分配，实测导致
# "HTTP API thread fail: can't create thread"。延迟 import 后只加载需要的那个。
HAS_MODBUS_TCP = False
HAS_MODBUS = False
ModbusTCPMaster = None
ModbusMaster = None

# MicroPython 的 _thread 模块（v6.0.5 起 HTTP 已回主循环，现仅 Modbus worker
# 线程用；缺时自动跳过。stack_size 仍在 start_control_http 里统一设定）
try:
    import _thread
except ImportError:
    _thread = None

# -------------------- 硬件配置（按实际板子修改） --------------------
RELAY_PINS = [3, 4, 5, 7]                   # 4 路继电器 GPIO（低电平吸合）
SW1_PIN = 10                                # SW1 按键 = IO10（作业规格：长按 5 秒进配网）
SW_PIN = 9                                  # SW 按键 = IO9（板上 BOOT 按钮同脚；运行时为普通 GPIO，短按循环继电器、长按全部关闭）
LED_PIN = 2                                  # 状态指示灯 GPIO（IO2）
AP_SSID = "Relay4-Setuplfx"                # 配网热点名称（加了 lfx 后缀避免和别人板子冲突）
AP_IP = "192.168.4.1"

# -------------------- 运行参数 --------------------
WIFI_TIMEOUT_S = 45          # 上电连 WiFi 超时时间（单次 1001 connecting 等待预算；
                             # REPL 实证 esp-sha 瞬时内存压力下驱动自愈最长 ~40s）
LONG_PRESS_MS = 5000         # 长按 SW1 进入配网模式时间
SW_LONG_PRESS_MS = 3000      # 长按 SW 时间（全关所有继电器）
RETRY_S = 5                  # WiFi/MQTT 断线重连周期
MQTT_KEEPALIVE = 45          # MQTT 保活（v6.0.4: 60->45，broker 更快判出半死连接并触发 will）
MQTT_PING_S = 15             # 主动 PING 周期（v6.0.4: 30->15，加速暴露已死的 TCP 写通道）
REPORT_INTERVAL_S = 5        # 属性上报周期
CHANNEL_COUNT = 4

# -------------------- v6.0.4 MQTT 链路自愈 --------------------
# 根因：umqtt qos=1 publish 阻塞等 PUBACK，而回调在 wait_msg() 内派发 —— 心跳
# 下行触发的嵌套 publish 会把外层等的 PUBACK 吃掉，外层永久卡死（心跳仍正常、
# 属性停滞、/api/relay 全超时）。修复：出站一律 qos=0（不读 PUBACK，嵌套安全）；
# 订阅侧保持 qos=1。完整推演见 ROOTCAUSES.md 第 1 条。
PUB_QOS = 0                          # v6.0.4：出站发布 QoS（0 = 不等待 PUBACK，防死锁）

# bridge 定时下发 functionId=__hb__ 心跳，payload 带 stall_s（bridge 眼中属性停滞
# 秒数）——接收端把真相回传，板子据此自愈：超 HEARTBEAT_TIMEOUT_S 没收到心跳、
# 连续 PUB_FAIL_LIMIT 次 publish 抛错、bridge 报 stall_s >= STALL_RECONNECT_S
# 三种情况都主动断开重连。
HEARTBEAT_FUNCTION_ID = "__hb__"     # 与 bridge config.yaml 的 function_id 一致
HEARTBEAT_TIMEOUT_S = 180            # 心跳超时（秒）；可在 config.json 用 hb_timeout_s 覆盖
PUB_FAIL_LIMIT = 3                   # 连续 publish 失败次数上限 -> 强制重连
STALL_RECONNECT_S = 60               # bridge 报告的属性停滞秒数阈值 -> 强制重连
# 刚连上时的宽限期：bridge 侧的「属性停滞」在板子重连瞬间仍是旧值，若立刻照做
# 会形成「连上-重连」抖动。必须同时满足「本连接已稳定 CONN_GRACE_S 秒」
# 且「本连接已 ACK 过 2 次心跳」才认这条指令。
CONN_GRACE_S = 30

# ---- v6.0.5 自愈 D：WiFi 数据面假死（isconnected() 真但收发包全丢）----
# 症状：isconnected()=True / status=1010 / 有合法 IP，但 MQTT 建连
# [Errno 113] ECONNABORTED，PC 侧 ping 大量丢包。主循环原「WiFi 断线重连」
# 只在 isconnected()=False 时触发 -> 假死下恒 True，会永远卡在 MQTT 重试。
# 对策：连续建连失败 >= 阈值且仍自称已连接 -> disconnect()+connect() 重组。
MQTT_FAIL_REASSOC = 3                # 连续 MQTT 建连失败达到该值 -> 触发重新关联
WIFI_REASSOC_COOLDOWN_MS = 90000     # 两次重新关联之间的最小间隔

# ---- v6.0.5 自愈 E：网络彻底不可用 -> 硬复位，回退到 boot 空堆预连 ----
# 实板出现过「自愈 D 捅完后停在 IDLE 再也不连」的死局，只能人工断电。捅不动就
# 换更粗的锤子：machine.reset() -> 回到 boot.py 空堆预连（最可靠的起点）。
# 三重与门防复位风暴：曾连上过 + 不可用超 NET_RESET_AFTER_MS + 本轮重组超
# NET_RESET_MIN_REASSOCS 次。复位后若仍连不上则本分支不再成立，交回 portal 守卫。
NET_RESET_AFTER_MS = 180000          # MQTT 连续不可用达到该时长 -> 考虑硬复位
NET_RESET_MIN_REASSOCS = 2           # 且本轮不可用期间至少重新关联过这么多次

# ---- v6.0.5 建连超时预检：别让阻塞 connect() 饿死主循环 ----
# 链路差时阻塞 connect() 要等 lwIP SYN 重传耗尽（单次可达 ~55s，实测 210s 只
# 跑完 3 次建连），期间主循环停摆。故建连前先用带 settimeout 的 socket 预检。
MQTT_CONNECT_TIMEOUT_S = 3           # TCP 预检超时（秒）

CONFIG_PATH = "config.json"
# 软复位后直接进 portal 的一次性标志文件：
# STA 热切换进 AP 会因多线程同时持网络资源触发 lwIP 崩溃（Guru Meditation），
# 改为写此标志 -> machine.reset() -> 上电干净环境（无线程）检测到标志直接进配网。
PORTAL_FLAG = "/portal.flag"

# 默认 Modbus TCP 采集配置示例：从站 1 的 0x0000 -> temperature, 0x0001 -> humidity
DEFAULT_MODBUS_CONFIG = {
    "enabled": True,
    "mode": "tcp",
    "timeout_ms": 500,
    "retries": 2,
    "retry_interval_ms": 500,
    "slaves": [
        {
            "enabled": True,
            "host": "192.168.20.59",
            "port": 5502,
            "unit_id": 1,
            "registers": [
                {"addr": 0, "func": 3, "key": "temperature", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2, "writable": False, "product": ""},
                {"addr": 1, "func": 3, "key": "humidity", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2, "writable": False, "product": ""},
            ]
        }
    ]
}

DEFAULT_CONFIG = {
    "wifi_ssid": "",
    "wifi_password": "",
    "mqtt_host": "172.16.4.211",
    "mqtt_port": 9783,
    "mqtt_user": "test",
    "mqtt_password": "123456",
    "product_id": "relay4_lfx",
    "device_id": "",
    "report_interval": REPORT_INTERVAL_S,
    "topic_mode": "direct",   # "direct"=沿用 EMQX 规则路径; "sys"=JetLinks 规范 /sys/... 路径
    "relay_pins": RELAY_PINS,
    "sw1_pin": SW1_PIN,
    "sw_pin": SW_PIN,
    "modbus": DEFAULT_MODBUS_CONFIG,
}

# v6.0.4：可在 config.json 里覆盖、但不暴露在配网页表单上的高级参数
PASSTHROUGH_KEYS = ("hb_timeout_s", "stall_reconnect_s")

# -------------------- 全局对象 --------------------
relays = []
button = None              # SW1 Pin 对象（IO10，长按 5 秒进配网）
sw_button = None           # SW Pin 对象（IO9，短按循环、长按全关）
led = None
wlan_sta = None
wlan_ap = None
_sta_was_active = False  # 本进程是否曾对 STA 调过 active(True)；portal 收尾只碰启动过的接口
_sta_connected_once = False  # 本进程 STA 是否曾连上 WiFi（区分"运行中主动进 portal"与"失败兜底进 portal"）
_wifi_fail_streak = 0        # 本进程 WiFi 失败累计（超时/give up 时+1）；仅供日志参考
_wifi_failed_this_boot = False  # 本进程 WiFi 是否失败过（守卫据此决定不进 AP 而复位重试）
client = None
topics = None
mb_master = None
# v6.0.5：HTTP 控制 API 改为「主循环非阻塞轮询」，不再开独立线程
_http_srv = None             # HTTP API 监听 socket（主线程持有）
_http_stop = False           # 停止标志（portal 切换前关闭监听 socket）
_http_conn = None            # 当前正在读取的客户端连接（单连接状态机）
_http_buf = b""              # 当前连接已读入的请求字节
_http_deadline_ms = 0        # 当前连接的读超时截止（ticks_ms）
_http_polls = 0              # 主循环 HTTP 轮询次数（诊断）
_http_reqs = 0               # 已完成（读全并处理）的 HTTP 请求数（诊断）
_http_aborts = 0             # 半开/超时/超长被丢弃的连接数（诊断）
last_ping = 0
last_report = 0
mqtt_retry = 0
wifi_retry = 0
# v6.0.4 MQTT 链路自愈诊断
last_hb_ms = 0               # 最近一次收到网关心跳(__hb__)的时间
hb_acks = 0                  # 已回给 bridge 的心跳 ACK 数（累计）
hb_acks_conn = 0             # 本连接内已 ACK 的心跳数（重连即清零，用于 stall 指令宽限）
pub_fail_streak = 0          # 连续 publish 失败计数
mqtt_connects = 0            # MQTT 成功建连次数
conn_ms = 0                  # 本连接建立时刻（ticks_ms），用于 stall 指令宽限
# v6.0.5 自愈 D：WiFi 数据面假死诊断
mqtt_fail_streak = 0         # 连续 MQTT 建连失败次数（成功即清零）
wifi_reassocs = 0            # 已主动触发的「重新关联」次数（诊断）
wifi_reassoc_ms = None       # 上次重新关联时刻（ticks_ms）；None=从未触发，冷却门据此放行
# v6.0.5 自愈 E：网络彻底不可用 -> 硬复位
net_dead_since = 0           # 本轮「MQTT 不可用」起点（ticks_ms）；0=当前链路正常
net_dead_base_reassocs = 0   # 进入本轮不可用时刻的 wifi_reassocs 基线（算"本轮捅了几次"）
net_resets = 0               # 已因网络不可用发起的硬复位次数（诊断）
stall_cmds = 0               # 收到「bridge 报告属性停滞」指令的次数
stall_reconnect_active_s = STALL_RECONNECT_S  # 实际生效的停滞阈值（config.json 可覆盖）
force_reconnect = False      # 需要主动断开重连（主循环消费）
report_requested = False     # HTTP 请求的一次性上报（主循环消费，替代跨线程 publish）
hb_timeout_active_s = HEARTBEAT_TIMEOUT_S   # 实际生效的心跳超时（config.json 可覆盖）
button_state = 1
press_start = 0
long_triggered = False
short_triggered = False
short_requested = False   # HTTP 短按请求标志（主循环消费）
portal_requested = False  # HTTP / GPIO 共用的"请求进入 AP 配网"标志

# SW（IO9）按键状态
sw_button_state = 1
sw_press_start = 0
sw_long_triggered = False
sw_short_triggered = False
sw_short_requested = False  # SW 短按触发后请求立即 publish
sw_cycle_idx = 0            # SW 短按循环计数器：0=CH1, 1=CH2, 2=CH3, 3=CH4, 4=全关, 然后回到 0
SW_CYCLE = [0, 1, 2, 3, "all"]  # SW 短按循环目标

# -------------------- 工具函数 --------------------
def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = dict(DEFAULT_CONFIG)
    out.update({k: cfg[k] for k in DEFAULT_CONFIG if k in cfg})
    # v6.0.4：高级调参项透传——不进 DEFAULT_CONFIG（不在配网页表单里给普通用户
    # 误改），但允许预置在 config.json 覆盖默认值，/save 回写时保留。
    for _k in PASSTHROUGH_KEYS:
        if _k in cfg:
            out[_k] = cfg[_k]
    # Modbus 配置兼容：无 mode 字段时按 uart_id 判断旧 RTU，否则默认 tcp
    mb = out.get("modbus") or DEFAULT_MODBUS_CONFIG
    if isinstance(mb, dict) and not mb.get("mode"):
        if mb.get("uart_id") is not None:
            mb["mode"] = "rtu"
        else:
            mb["mode"] = "tcp"
        out["modbus"] = mb
    if not out["device_id"]:
        out["device_id"] = default_device_id()
    if not out["mqtt_user"]:
        out["mqtt_user"] = out["device_id"]
    return out


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


def default_device_id():
    """默认设备 ID 读取 STA MAC 地址"""
    try:
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        mac = sta.config("mac")
        sta.active(False)
        return "".join("%02x" % b for b in mac)
    except Exception:
        return "relay4"


def mac_str():
    try:
        sta = network.WLAN(network.STA_IF)
        active = sta.active()
        if not active:
            sta.active(True)
        mac = sta.config("mac")
        if not active:
            sta.active(False)
        return ":".join("%02x" % b for b in mac)
    except Exception:
        return "unknown"


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def now_ms():
    return time.ticks_ms()


def fmt_now():
    # MicroPython 没有 time.strftime，用简单格式
    t = time.localtime()
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


# -------------------- 继电器控制 --------------------
def init_relays(cfg_pins):
    global relays
    pins = cfg_pins if isinstance(cfg_pins, list) and len(cfg_pins) >= CHANNEL_COUNT else RELAY_PINS
    relays = [Pin(int(pins[i]), Pin.OUT, value=1) for i in range(CHANNEL_COUNT)]  # 1=断开，安全默认


def relay_is_on(idx):
    return relays[idx].value() == 0


def set_relay(idx, on):
    """设置单路，返回是否发生变化"""
    on = to_bool(on)
    if idx < 0 or idx >= CHANNEL_COUNT:
        return False
    if relay_is_on(idx) == on:
        return False
    relays[idx].value(0 if on else 1)
    return True


def set_all_relay(on):
    changed = []
    for i in range(CHANNEL_COUNT):
        if set_relay(i, on):
            changed.append(i)
    return changed


# -------------------- 属性/事件/回复构造 --------------------
class ChannelModel:
    """简单模拟电压/电流/功率；真实硬件没有传感器时给出占位值"""
    def __init__(self):
        self.temperature = 26.0
        self.humidity = 50.0
        # 为每路给一个略有差异的负载电流
        self.i_base = [0.05 + (i % 3) * 0.05 for i in range(CHANNEL_COUNT)]

    def tick(self):
        import random
        self.temperature += random.uniform(-0.05, 0.05)
        self.humidity += random.uniform(-0.2, 0.2)
        self.temperature = min(35.0, max(20.0, self.temperature))
        self.humidity = min(70.0, max(30.0, self.humidity))

    def snapshot(self):
        import random
        self.tick()
        props = {}
        for i in range(CHANNEL_COUNT):
            on = relay_is_on(i)
            props["ch%d_state" % (i + 1)] = on
            if on:
                v = 220.0 + random.uniform(-1.0, 1.0)
                i_val = self.i_base[i] * random.uniform(0.95, 1.05)
                props["ch%d_voltage" % (i + 1)] = round(v, 2)
                props["ch%d_current" % (i + 1)] = round(i_val, 2)
                props["ch%d_power" % (i + 1)] = round(v * i_val, 2)
            else:
                props["ch%d_voltage" % (i + 1)] = 0.0
                props["ch%d_current" % (i + 1)] = 0.0
                props["ch%d_power" % (i + 1)] = 0.0
        props["temperature"] = round(self.temperature, 2)
        props["humidity"] = round(self.humidity, 2)
        return props


channel_model = ChannelModel()


def build_property_payload(cfg):
    props = channel_model.snapshot()
    # 合并 Modbus 采集值：独立线程在后台轮询，不会阻塞继电器控制
    if mb_master is not None:
        try:
            mb_values = mb_master.get_values()
            for full_key, val in mb_values.items():
                # 完整 key（如 s1_temperature）保留，方便定位是哪一台从站
                props[full_key] = val
                # 同时按用户配置的原始 key（去掉 sN_ 前缀）上报到物模型
                if full_key.startswith("s"):
                    # 找到第一个下划线
                    try:
                        underscore = full_key.index("_")
                        raw_key = full_key[underscore + 1:]
                        if raw_key:
                            props[raw_key] = val
                    except ValueError:
                        pass
            if mb_values:
                print("[MAIN] merged modbus values into property report:", list(mb_values.keys()))
        except Exception as e:
            print("[MAIN] merge modbus values error:", e)
    return {
        "productId": cfg["product_id"],
        "deviceId": cfg["device_id"],
        "timestamp": int(time.time() * 1000),
        "properties": props,
    }


def build_event_payload(cfg, channel, state):
    return {
        "productId": cfg["product_id"],
        "deviceId": cfg["device_id"],
        "timestamp": int(time.time() * 1000),
        "eventId": "switch_change",
        "data": {"channel": channel, "state": bool(state)},
    }


def build_reply_payload(message_id, success, output=True):
    return {"messageId": message_id, "success": bool(success), "output": bool(output)}


# -------------------- Topic 管理 --------------------
class TopicManager:
    def __init__(self, product_id, device_id, mode="direct"):
        self.mode = mode
        self.base = "/sys/%s/%s" % (product_id, device_id) if mode == "sys" else "/%s/%s" % (product_id, device_id)

    def property_post(self):
        return self.base + ("/thing/event/property/post" if self.mode == "sys" else "/property/post")

    def property_set(self):
        """JetLinks 属性写（ direct 模式下对应服务命令 /service/cmd）"""
        return self.base + ("/thing/service/property/set" if self.mode == "sys" else "/service/cmd")

    def property_set_reply(self):
        return self.base + ("/thing/service/property/set_reply" if self.mode == "sys" else "/function/post")

    def function_invoke(self, function_id):
        return "%s/thing/service/%s/invoke" % (self.base, function_id)

    def function_reply(self, function_id):
        return "%s/thing/service/%s/invoke/reply" % (self.base, function_id)

    def function_invoke_wildcard(self):
        """订阅所有功能调用：/sys/.../thing/service/+/invoke"""
        return "%s/thing/service/+/invoke" % self.base

    def event(self, event_id):
        return self.base + ("/thing/event/%s" % event_id if self.mode == "sys" else "/event/%s" % event_id)

    def online(self):
        return self.base + "/online"

    def offline(self):
        return self.base + "/offline"

    def properties_read(self):
        return self.base + ("/thing/service/property/read" if self.mode == "sys" else "/properties/read")

    def properties_write(self):
        return self.base + ("/thing/service/property/write" if self.mode == "sys" else "/properties/write")

    def properties_read_reply(self):
        return self.base + ("/thing/service/property/read/reply" if self.mode == "sys" else "/properties/read/reply")

    def properties_write_reply(self):
        return self.base + ("/thing/service/property/write/reply" if self.mode == "sys" else "/properties/write/reply")


# -------------------- MQTT --------------------
def _tcp_preflight(host, port, timeout_s):
    """带超时的 TCP 可达性预检。

    socket settimeout 后 connect() 走非阻塞+poll，到点抛 ETIMEDOUT，故能把
    「等 SYN 重传耗尽」的最坏 ~55s 压到 timeout_s。返回 True = TCP 可达。
    """
    s = None
    try:
        s = socket.socket()
        s.settimeout(timeout_s)
        s.connect(socket.getaddrinfo(host, port)[0][-1])
        return True
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def mqtt_connect(cfg):
    global client, topics, last_ping, last_report, mqtt_retry
    global last_hb_ms, pub_fail_streak, mqtt_connects
    global hb_acks_conn, conn_ms, mqtt_fail_streak
    from umqtt.simple import MQTTClient
    topics = TopicManager(cfg["product_id"], cfg["device_id"], cfg.get("topic_mode", "direct"))
    print("MQTT topics base:", topics.base)
    try:
        import gc
        # umqtt.simple import + socket 建立前回收碎片（C3 堆紧张，实测
        # 不 collect 时偶发 ECONNABORTED/ENOMEM 建连失败）
        gc.collect()
        # v6.0.5：先做带超时的 TCP 预检，绝不让阻塞 connect 把主循环拖住几十秒。
        if not _tcp_preflight(cfg["mqtt_host"], int(cfg["mqtt_port"]),
                              MQTT_CONNECT_TIMEOUT_S):
            raise OSError(110, "preflight: tcp %s:%s unreachable in %ss" % (
                cfg["mqtt_host"], cfg["mqtt_port"], MQTT_CONNECT_TIMEOUT_S))
        # umqtt.simple 要求 bytes：topic/payload/client_id 均显式编码
        c = MQTTClient(
            cfg["device_id"].encode("utf-8"),
            cfg["mqtt_host"],
            int(cfg["mqtt_port"]),
            user=(cfg["mqtt_user"] or "").encode("utf-8") or None,
            password=(cfg["mqtt_password"] or "").encode("utf-8") or None,
            keepalive=MQTT_KEEPALIVE,
        )
        c.set_callback(mqtt_message)
        c.set_last_will(topics.offline().encode("utf-8"),
                        json.dumps({"deviceId": cfg["device_id"]}).encode("utf-8"),
                        retain=True, qos=PUB_QOS)
        c.connect(clean_session=True)
        if cfg.get("topic_mode", "direct") == "sys":
            c.subscribe(topics.function_invoke_wildcard().encode("utf-8"), qos=1)
            c.subscribe(topics.properties_read().encode("utf-8"), qos=1)
            c.subscribe(topics.properties_write().encode("utf-8"), qos=1)
        else:
            c.subscribe(topics.property_set().encode("utf-8"), qos=1)
            c.subscribe(topics.properties_read().encode("utf-8"), qos=1)
            c.subscribe(topics.properties_write().encode("utf-8"), qos=1)
        client = c
        last_ping = now_ms()
        last_report = 0  # 连上后立即触发一次上报
        mqtt_retry = 0
        # v6.0.4：新连接给心跳一个宽限期，避免刚连上就被 watchdog 判死
        last_hb_ms = now_ms()
        hb_acks_conn = 0     # 本连接内的心跳 ACK 计数（stall 指令宽限用）
        conn_ms = now_ms()   # 本连接建立时刻
        pub_fail_streak = 0
        mqtt_fail_streak = 0     # v6.0.5 自愈 D：建连成功即清零
        mqtt_connects += 1
        print("MQTT connected:", cfg["mqtt_host"])
        c.publish(topics.online().encode("utf-8"),
                  json.dumps({"deviceId": cfg["device_id"]}).encode("utf-8"),
                  retain=True, qos=PUB_QOS)
        publish_property(cfg)
        return True
    except Exception as e:
        print("MQTT connect failed:", e)
        mqtt_fail_streak += 1        # v6.0.5 自愈 D：累计连续建连失败
        client = None
        mqtt_retry = time.ticks_add(now_ms(), RETRY_S * 1000)
        return False


def mqtt_disconnect():
    global client
    if client is not None:
        try:
            client.disconnect()
        except Exception:
            pass
        client = None


def _mark_pub_fail(where):
    """v6.0.4：记一次发布失败，主循环据此判定是否需要强制重连。"""
    global pub_fail_streak
    pub_fail_streak += 1
    print("[MQTT] pub fail streak=%d (%s)" % (pub_fail_streak, where))
    return pub_fail_streak


def _mark_pub_ok():
    global pub_fail_streak
    pub_fail_streak = 0


def publish_property(cfg, force=False):
    if client is None:
        return
    try:
        payload = build_property_payload(cfg)
        client.publish(topics.property_post().encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
        _mark_pub_ok()
        print("property posted")
    except Exception as e:
        print("property post error:", e)
        _mark_pub_fail("property")
        raise


def publish_heartbeat_ack(cfg, seq):
    """v6.0.4：回应 bridge 下发的网关心跳。

    走事件通道（direct 模式 = /{pid}/{did}/event/hb_ack），bridge 已订阅
    .../event/+，收到即知板子「收 + 发」两个方向都活着——这正是 bridge
    单看上行属性无法判断的（半死 socket 下 publish 静默丢失，不报错）。
    """
    global hb_acks, hb_acks_conn
    if client is None:
        return
    try:
        payload = {
            "productId": cfg["product_id"],
            "deviceId": cfg["device_id"],
            "timestamp": int(time.time() * 1000),
            "eventId": "hb_ack",
            "data": {"seq": seq},
        }
        client.publish(topics.event("hb_ack").encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
        _mark_pub_ok()
        hb_acks += 1
        hb_acks_conn += 1
        print("[MQTT] hb_ack #%d seq=%s" % (hb_acks, seq))
    except Exception as e:
        print("[MQTT] hb_ack publish error:", e)
        _mark_pub_fail("hb_ack")


def note_heartbeat(cfg, data):
    """v6.0.4：收到网关心跳 -> 刷新存活时间戳、回 ACK，并解析 bridge 回传的停滞时长。

    板子自己无法察觉「上行 publish 被静默丢弃」（既不抛异常，心跳 ACK 也照常往返
    ——见文件头 2026-09-10 根因记录）。只有接收端 bridge 知道父设备属性断了多久，
    所以由它在心跳 payload 里带 stall_s，这里超阈值即置 force_reconnect，
    交主循环断开重连。这是「silent socket」类故障唯一的可探测点。
    """
    global last_hb_ms, force_reconnect, stall_cmds
    last_hb_ms = now_ms()
    publish_heartbeat_ack(cfg, data.get("messageId") or "")
    try:
        stall_s = int(data.get("stall_s", 0) or 0)
    except (TypeError, ValueError):
        stall_s = 0
    if stall_s >= stall_reconnect_active_s and not force_reconnect:
        # 宽限期：刚重连时 bridge 侧的停滞读数还是旧连接的，立刻照做会「连上→重连」抖动
        conn_age_ms = time.ticks_diff(now_ms(), conn_ms)
        if hb_acks_conn >= 2 and conn_age_ms >= CONN_GRACE_S * 1000:
            stall_cmds += 1
            force_reconnect = True
            print("[MQTT] bridge reports upstream stalled %ds -> force reconnect" % stall_s)
        else:
            print("[MQTT] stall_s=%ds deferred (conn_age=%ds acks_conn=%d)"
                  % (stall_s, conn_age_ms // 1000, hb_acks_conn))


def publish_event(cfg, channel, state):
    if client is None:
        return
    try:
        payload = build_event_payload(cfg, channel, state)
        client.publish(topics.event("switch_change").encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
        _mark_pub_ok()
        print("event switch_change ch%d=%s" % (channel, state))
    except Exception as e:
        print("event post error:", e)
        _mark_pub_fail("event")
        raise


def publish_reply(cfg, message_id, success, output=True, function_id=None):
    if client is None:
        return
    try:
        payload = build_reply_payload(message_id, success, output)
        if cfg.get("topic_mode", "direct") == "sys" and function_id:
            reply_topic = topics.function_reply(function_id)
        else:
            reply_topic = topics.property_set_reply()
        client.publish(reply_topic.encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
    except Exception as e:
        print("reply post error:", e)
        raise


def publish_read_reply(cfg, message_id, props):
    if client is None:
        return
    try:
        payload = {
            "productId": cfg["product_id"],
            "deviceId": cfg["device_id"],
            "messageId": message_id,
            "success": True,
            "properties": props,
        }
        client.publish(topics.properties_read_reply().encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
    except Exception as e:
        print("read reply error:", e)
        raise


def publish_write_reply(cfg, message_id):
    if client is None:
        return
    try:
        payload = {
            "productId": cfg["product_id"],
            "deviceId": cfg["device_id"],
            "messageId": message_id,
            "success": True,
        }
        client.publish(topics.properties_write_reply().encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=PUB_QOS)
    except Exception as e:
        print("write reply error:", e)
        raise


def mqtt_message(topic, payload):
    global client
    try:
        # MicroPython umqtt.simple 回调收到的是 bytes，统一解码为 str 处理
        if isinstance(topic, bytes):
            topic = topic.decode("utf-8")
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        msg = payload
        data = json.loads(msg)
        print("recv topic:", topic, "msg:", msg[:200])
    except Exception as e:
        print("mqtt msg decode error:", e)
        return
    try:
        if topics is None:
            return
        cfg = load_config()  # 取最新配置（模块级无全局 cfg）
        if topic == topics.property_set() and cfg.get("topic_mode", "direct") == "direct":
            # direct 模式：所有服务命令都走 /service/cmd
            handle_command(data)
        elif topic.startswith(topics.base + "/thing/service/") and topic.endswith("/invoke"):
            # sys 模式功能调用：/sys/.../thing/service/{functionId}/invoke
            func_id = topic[len(topics.base + "/thing/service/"):-len("/invoke")]
            if func_id:
                handle_command(data, function_id=func_id)
        elif topic == topics.properties_read():
            handle_read_property(data)
        elif topic == topics.properties_write():
            handle_write_property(data)
    except Exception as e:
        print("handle msg error:", e)
        # v6.0.4：下行处理里的 OSError 基本等同于 socket 已坏，计入失败计数
        if isinstance(e, OSError):
            _mark_pub_fail("downlink-handler")


# -------------------- 命令解析 --------------------
def parse_inputs(data):
    """JetLinks 命令 inputs [{name,value}] / params {name:value}"""
    out = {}
    inputs = data.get("inputs")
    if isinstance(inputs, list):
        for it in inputs:
            if isinstance(it, dict) and it.get("name") is not None:
                out[str(it["name"])] = it.get("value")
    if isinstance(data.get("params"), dict):
        out.update(data["params"])
    return out


def handle_command(data, function_id=None):
    if not isinstance(data, dict):
        return
    cfg = load_config()
    message_id = data.get("messageId") or data.get("id") or ""
    method = function_id or data.get("functionId") or data.get("method") or ""
    # v6.0.4 网关心跳：不参与继电器逻辑，只刷新存活时间戳并立即回 ACK
    if method == HEARTBEAT_FUNCTION_ID:
        note_heartbeat(cfg, data)
        return
    args = parse_inputs(data)
    print("handle command:", method, args)

    changed = []
    ok = True
    if method == "set_channel":
        try:
            ch = int(args.get("channel"))
        except (TypeError, ValueError):
            publish_reply(cfg, message_id, False, False, function_id)
            return
        state = args.get("state")
        if state is None or not (1 <= ch <= CHANNEL_COUNT):
            publish_reply(cfg, message_id, False, False, function_id)
            return
        if set_relay(ch - 1, state):
            changed.append(ch)
    elif method == "switch_all":
        state = args.get("state")
        if state is None:
            publish_reply(cfg, message_id, False, False, function_id)
            return
        changed = set_all_relay(state)
    elif method == "write_register":
        # Modbus TCP 写单个保持寄存器（功能码 06）
        try:
            slave_idx = int(args.get("slave_idx", 0))
            addr = int(args.get("addr", 0))
            value = int(args.get("value", 0))
        except (TypeError, ValueError):
            publish_reply(cfg, message_id, False, False, function_id)
            return
        mb_cfg = cfg.get("modbus") or DEFAULT_MODBUS_CONFIG
        slaves = mb_cfg.get("slaves", [])
        if not (0 <= slave_idx < len(slaves)):
            publish_reply(cfg, message_id, False, False, function_id)
            return
        if mb_master is None or not hasattr(mb_master, "write_register"):
            publish_reply(cfg, message_id, False, False, function_id)
            return
        ok, err = mb_master.write_register(slave_idx, slaves[slave_idx], addr, value)
        if not ok:
            print("[MAIN] write_register failed:", err)
        publish_reply(cfg, message_id, ok, ok, function_id)
        return
    else:
        print("unknown function:", method)
        publish_reply(cfg, message_id, False, False, function_id)
        return

    # 有变化：立即属性上报 + 每个变化通道发事件
    if changed:
        publish_property(cfg)
        for ch in changed:
            publish_event(cfg, ch, relay_is_on(ch - 1))
    else:
        publish_property(cfg)
    publish_reply(cfg, message_id, ok, True, function_id)


def handle_read_property(data):
    cfg = load_config()
    ids = data.get("properties") or []
    want = set()
    for it in ids:
        if isinstance(it, str):
            want.add(it)
        elif isinstance(it, dict) and it.get("id"):
            want.add(it["id"])
    snap = channel_model.snapshot()
    props = {k: v for k, v in snap.items() if not want or k in want}
    message_id = data.get("messageId")
    if message_id:
        publish_read_reply(cfg, message_id, props)


def handle_write_property(data):
    cfg = load_config()
    props = data.get("properties") or {}
    if not isinstance(props, dict):
        return
    changed = []
    for key, val in props.items():
        m = None
        if key.startswith("ch") and key.endswith("_state"):
            try:
                m = int(key[2:-6])
            except ValueError:
                pass
        elif key.startswith("r") and key[1:].isdigit():
            m = int(key[1:])
        if m and 1 <= m <= CHANNEL_COUNT:
            if set_relay(m - 1, val):
                changed.append(m)
    if changed:
        publish_property(cfg)
        for ch in changed:
            publish_event(cfg, ch, relay_is_on(ch - 1))
    message_id = data.get("messageId")
    if message_id:
        publish_write_reply(cfg, message_id)


# -------------------- Web 配网 --------------------



def unquote_plus(s):
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == "+":
            out += b" "
            i += 1
        elif c == "%" and i + 2 < len(s):
            try:
                out.append(int(s[i + 1:i + 3], 16))
            except ValueError:
                out.append(ord("%"))
            i += 3
        else:
            out.append(ord(c))
            i += 1
    return out.decode("utf-8", "replace")


def parse_form(body):
    out = {}
    for kv in body.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[unquote_plus(k)] = unquote_plus(v)
    return out


def http_send(conn, status, body, ctype="text/html"):
    conn.send(("HTTP/1.0 %s\r\nContent-Type: %s; charset=utf-8\r\nConnection: close\r\n\r\n%s" % (status, ctype, body)).encode("utf-8"))
    try:
        conn.close()
    except Exception:
        pass


def render_page(cfg):
    # 读取 portal.html 配网页（v5.2 起外置为独立文件：15KB 页面字符串不再常驻
    # RAM heap，避免挤压 WiFi 驱动内存导致 WPA2 握手失败/热切换崩溃）
    try:
        with open("portal.html", "r") as _f:
            page = _f.read()
    except Exception:
        page = "<html><body><h2>portal.html missing on device</h2></body></html>"
    # 只填充模板需要的字段，避免 relay_pins/sw1_pin 这些硬件常量
    # 触发 str.format "extra keyword arguments given" 让 portal 循环崩
    keys = ("wifi_ssid", "wifi_password",
            "mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password",
            "product_id", "device_id", "report_interval")
    d = {}
    for k in keys:
        v = cfg.get(k, DEFAULT_CONFIG[k])
        d[k] = str(v).replace('"', "&quot;")
    # MicroPython json.dumps 不支持 ensure_ascii 参数（CPython 有）
    try:
        mb_json = json.dumps(cfg.get("modbus", DEFAULT_MODBUS_CONFIG))
    except Exception:
        mb_json = json.dumps(DEFAULT_MODBUS_CONFIG)
    # 作为 hidden input 的 value 属性，需要转义双引号避免 HTML 属性截断
    d["modbus_json"] = mb_json.replace('"', "&quot;")
    d["mac"] = mac_str()
    d["sel_direct"] = 'selected' if cfg.get("topic_mode", "direct") == "direct" else ""
    d["sel_sys"] = 'selected' if cfg.get("topic_mode", "direct") == "sys" else ""
    return page.format(**d)


def _parse_query(path):
    """解析 query string 到 dict（仅供 HTTP API 用，不处理 urlencoding 全套）"""
    q = {}
    if "?" not in path:
        return q
    qs = path.split("?", 1)[1]
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            q[k] = v
    return q


def _http_json(conn, code, obj):
    """200 OK + JSON body"""
    body = json.dumps(obj)
    http_send(conn, code, body, "application/json")


def http_api_handler(cfg, conn):
    """HTTP 控制 API 路由。

    v6.0.4：本函数曾运行在 HTTP 线程，**绝不能直接调用任何 publish_*()**。
    原因有两层：(1) 与主循环的 check_msg/ping 抢同一个 MQTT socket；
    (2) umqtt publish 内部会 wait_msg 读 socket，两个线程同时读会让 PUBACK
    被错误的线程吃掉。当时统一改为置标志，由主循环消费后 publish。

    v6.0.5：HTTP 服务已整体搬进主循环（http_poll 非阻塞轮询，见该函数说明），
    本函数现在**就在主循环线程里执行**，理论上可以直接 publish。但仍保留
    report_requested 标志：publish 统一由主循环的同一条路径发起，便于
    上报时序集中控制，也避免 handler 中途 publish 打乱本轮循环状态。

    路由：
    - GET /api/relay?ch=N&state=0/1  → 控制单路
    - GET /api/relay/all?state=0/1   → 全部控制
    - GET /api/status                → 当前 4 路状态
    - GET /api/info                  → IP/MAC/firmware/网络信息
    - GET /api/sw1?action=short     → 设置短按标志，由主循环消费后 publish（线程安全）
    - GET /api/sw1?action=long       → 模拟长按（不真进 portal，安全）
    - GET /api/sw1?action=long&real=1→ 真长按（踢 STA 进 AP 配网，自负风险）
    - GET /api/sw?action=short       → SW 短按：循环切换下一路继电器（CH1→CH2→CH3→CH4→全关）
    - GET /api/sw?action=long        → SW 长按：全部关闭，cycle 重置
    - GET /api/modbus_values         → 当前 Modbus 采集实时值
    """
    global report_requested
    try:
        head, _, rest = _read_request(conn)
    except Exception as e:
        http_send(conn, "400 Bad Request", "read err: %s" % e)
        return
    try:
        first = head.split(b"\r\n", 1)[0].decode("utf-8", "replace").split()
        method = first[0]
        path = first[1]
    except Exception as e:
        http_send(conn, "400 Bad Request", "bad req: %s" % e)
        return

    if method != "GET":
        http_send(conn, "405 Method Not Allowed", "GET only")
        return

    if path == "/api/status":
        snap = {}
        for i in range(1, CHANNEL_COUNT + 1):
            snap["ch%d" % i] = bool(relay_is_on(i - 1))
        _http_json(conn, "200 OK", {"ok": True, "channels": snap})
        return

    if path == "/api/info":
        info = {
            "ok": True,
            "device_id": cfg.get("device_id", ""),
            "mac": mac_str(),
            "wifi_connected": False,
            "ip": "",
            "sw1_pin": cfg.get("sw1_pin"),
            "sw_pin": cfg.get("sw_pin"),
            "sw_cycle_idx": sw_cycle_idx,
            # v6.0.4 MQTT 链路自愈诊断（判断板子是否处于半死 socket 状态）
            "mqtt_connected": client is not None,
            "mqtt_connects": mqtt_connects,
            "hb_acks": hb_acks,
            "hb_acks_conn": hb_acks_conn,
            "hb_age_s": (int(time.ticks_diff(now_ms(), last_hb_ms) / 1000)
                         if client is not None else None),
            "pub_fail_streak": pub_fail_streak,
            "pub_qos": PUB_QOS,
            "stall_cmds": stall_cmds,
            "force_reconnect": force_reconnect,
            "hb_timeout_s": hb_timeout_active_s,
            # v6.0.5 HTTP 主循环轮询诊断（观察并发下是否出现 abort 激增/请求停摆）
            "http_polls": _http_polls,
            "http_reqs": _http_reqs,
            "http_aborts": _http_aborts,
            "http_open": _http_conn is not None,
            # v6.0.5 自愈 D 诊断（isconnected 为真但数据面假死时的重新关联计数）
            "mqtt_fails": mqtt_fail_streak,
            "wifi_reassocs": wifi_reassocs,
            "wifi_reassoc_ms": wifi_reassoc_ms,
            # v6.0.5 自愈 E 诊断：MQTT 连续不可用秒数 / 已发起硬复位次数
            "net_dead_s": (int(time.ticks_diff(now_ms(), net_dead_since) / 1000)
                           if net_dead_since else 0),
            "net_resets": net_resets,
        }
        try:
            if wlan_sta and wlan_sta.isconnected():
                info["wifi_connected"] = True
                info["ip"] = wlan_sta.ifconfig()[0]
                # v6.0.5 链路质量诊断：「MQTT 建连 ECONNABORTED / ping 丢包」这类
                # 现象绝大多数是 RSSI 太低（板子位置/AP 干扰），一眼定性省得乱猜代码。
                try:
                    info["wifi_rssi"] = wlan_sta.status("rssi")
                except Exception:
                    pass
                try:
                    info["wifi_status"] = wlan_sta.status()
                except Exception:
                    pass
        except Exception:
            pass
        _http_json(conn, "200 OK", info)
        return

    if path == "/api/sw1" or path.startswith("/api/sw1?"):
        # 无杜邦线时用裸 HTTP 触发 SW1 的业务逻辑（短按/长按）
        q = _parse_query(path)
        action = (q.get("action", "") or "").lower()
        if action == "short":
            # 仅 set flag，主循环来 publish；HTTP 线程不直接碰 MQTT socket
            trigger_short_press(cfg, source="HTTP")
            payload = {"ok": True, "action": "short", "source": "HTTP", "queued": True}
            _http_json(conn, "200 OK", payload)
            return
        if action == "long":
            real = (q.get("real", "0") in ("1", "true", "yes"))
            enter_portal = bool(real)
            trigger_long_press(cfg, source="HTTP", enter_portal=enter_portal)
            payload = {
                "ok": True,
                "action": "long",
                "source": "HTTP",
                "simulated": not enter_portal,
                "portal_requested": bool(enter_portal),
            }
            _http_json(conn, "200 OK", payload)
            return
        _http_json(conn, "400 Bad Request",
                   {"ok": False, "err": "need ?action=short|long[&real=1]"})
        return

    if path.startswith("/api/relay"):
        q = _parse_query(path)
        try:
            if path.startswith("/api/relay/all"):
                state = int(q.get("state", 1))
                changed = set_all_relay(bool(state))
                _http_json(conn, "200 OK",
                           {"ok": True, "all": bool(state),
                            "changed": [i + 1 for i in changed]})
                # 触发一次主动上报（即使 broker 下行不通也能把状态同步上去）
                # v6.0.4：置标志交主循环 publish（HTTP 线程碰 MQTT socket 会死锁）
                report_requested = True
                return
            ch = int(q.get("ch", 0))
            state = q.get("state", None)
            if state is None or not (1 <= ch <= CHANNEL_COUNT):
                _http_json(conn, "400 Bad Request",
                           {"ok": False, "err": "need ch(1..4) and state(0/1)"})
                return
            ok = set_relay(ch - 1, int(state))
            _http_json(conn, "200 OK",
                       {"ok": True, "ch": ch, "state": int(state), "changed": ok})
            # v6.0.4：置标志交主循环 publish（HTTP 线程碰 MQTT socket 会死锁）
            report_requested = True
            return
        except Exception as e:
            _http_json(conn, "500 Internal Server Error",
                       {"ok": False, "err": str(e)})
            return

    if path == "/api/sw" or path.startswith("/api/sw?"):
        # SW (IO9) 短按/长按：短按循环切换继电器，长按全关
        q = _parse_query(path)
        action = (q.get("action", "") or "").lower()
        if action == "short":
            ok, err = trigger_sw_short(cfg, source="HTTP")
            payload = {
                "ok": ok, "action": "short", "source": "HTTP",
                "queued": True,
                "cycle_idx": sw_cycle_idx,
            }
            if err:
                payload["err"] = err
            _http_json(conn, "200 OK", payload)
            return
        if action == "long":
            trigger_sw_long(cfg, source="HTTP")
            payload = {
                "ok": True, "action": "long", "source": "HTTP",
                "queued": True,
                "cycle_reset_to": 0,
            }
            _http_json(conn, "200 OK", payload)
            return
        _http_json(conn, "400 Bad Request",
                   {"ok": False, "err": "need ?action=short|long"})
        return

    if path == "/api/modbus_values":
        values = {}
        if mb_master is not None:
            try:
                values = mb_master.get_values()
            except Exception as e:
                print("[HTTP API] get_values err:", e)
        _http_json(conn, "200 OK", {"ok": True, "values": values})
        return

    if path == "/api/modbus_write" or path.startswith("/api/modbus_write?"):
        # HTTP 调试：写单个保持寄存器 ?slave_idx=0&addr=0&value=123
        q = _parse_query(path)
        try:
            slave_idx = int(q.get("slave_idx", 0))
            addr = int(q.get("addr", 0))
            value = int(q.get("value", 0))
        except (TypeError, ValueError):
            _http_json(conn, "400 Bad Request",
                       {"ok": False, "err": "need slave_idx, addr, value"})
            return
        cfg = load_config()
        mb_cfg = cfg.get("modbus") or DEFAULT_MODBUS_CONFIG
        slaves = mb_cfg.get("slaves", [])
        if not (0 <= slave_idx < len(slaves)):
            _http_json(conn, "400 Bad Request",
                       {"ok": False, "err": "slave_idx out of range"})
            return
        if mb_master is None or not hasattr(mb_master, "write_register"):
            _http_json(conn, "503 Service Unavailable",
                       {"ok": False, "err": "modbus write not available"})
            return
        ok, err = mb_master.write_register(slave_idx, slaves[slave_idx], addr, value)
        _http_json(conn, "200 OK" if ok else "500 Internal Server Error",
                   {"ok": ok, "slave_idx": slave_idx, "addr": addr, "value": value, "err": err})
        return

    http_send(conn, "404 Not Found", "404 (try /api/status /api/info /api/relay?ch=1&state=1 /api/sw1?action=short /api/sw?action=short /api/modbus_values /api/modbus_write?slave_idx=0&addr=0&value=0)")


def _read_request(conn):
    """读 HTTP request head + body；返回 (head_bytes, sep, body_bytes)"""
    conn.settimeout(3)
    req = conn.recv(2048)
    head, _, rest = req.partition(b"\r\n\r\n")
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                clen = int(line.split(b":")[1].strip())
            except Exception:
                pass
    while len(rest) < clen:
        rest += conn.recv(1024)
    return head, b"\r\n\r\n", rest


# -------------------- v6.0.5 HTTP 服务（主循环非阻塞轮询） --------------------
# 【2026-09-10 实板事故】/api/relay 并发 >=4 整机静默冻结（ARP 不答、串口停住、
# 无 panic/复位，只能硬复位）。原因 = 旧 HTTP 独立线程与主循环并发操作 lwIP，
# 且 C3 内部 RAM 只允许 4KB 线程栈。修复 = HTTP 回主线程单连接状态机。
# 证据见 ROOTCAUSES.md 第 3 条。
HTTP_READ_TIMEOUT_MS = 3000   # 单个连接的请求读超时（读不满即丢弃，防慢连接拖死）
HTTP_FIRST_BYTE_TIMEOUT_MS = 1000  # 建连后首字节超时（半开连接 1s 内没数据即丢弃，
                                   # 否则它会占着单连接状态机、把后面的正常请求堵住）
HTTP_MAX_REQ_BYTES = 8192     # 单请求字节上限（防超大 body 撑爆 RAM）
HTTP_RECV_CHUNK = 1024        # 每轮最多读入的字节数（保证主循环单轮开销极小）
HTTP_LISTEN_BACKLOG = 8       # 监听队列长度（容忍突发并发；lwIP 每次连接仅占一个 PCB）
HTTP_MAX_CONN_PER_POLL = 4    # 单轮最多处理的「已就绪」连接数（限住主循环单轮开销）
EAGAIN_ERRNOS = (11, 10035)   # EAGAIN / EWOULDBLOCK（Windows 侧 WSAEWOULDBLOCK=10035）


class _HttpPrebufferedConn(object):
    """把「已读全的请求字节」伪装成 conn 交给 http_api_handler。

    v6.0.5：请求由 http_poll() 在主循环里非阻塞读全后，一次性喂给 handler。
    handler 内部仍会调 _read_request()，本包装让它：
      - settimeout() 变 no-op（轮询模式下不允许阻塞读）
      - recv() 只吐预读缓冲，耗尽后返回 b''（绝不阻塞、绝不碰真实 socket）
    其余方法（send/close/...）透传给真实连接。
    """

    def __init__(self, conn, prebuf):
        self._conn = conn
        self._buf = prebuf

    def settimeout(self, t):
        pass

    def recv(self, n):
        if not self._buf:
            return b""
        out = self._buf[:n]
        self._buf = self._buf[n:]
        return out

    def send(self, data):
        # 连接是非阻塞的：响应很小（<1KB，远小于 socket 发送缓冲），正常一次
        # 写完；万一遇到 EAGAIN 就短暂让出 CPU 重试（上限 ~200ms），避免
        # "响应被静默丢弃"和"长时间占住主循环"两个极端。
        sock = self._conn
        total = len(data)
        sent = 0
        tries = 0
        while sent < total:
            try:
                sent += sock.send(data[sent:])
            except OSError as e:
                eno = e.args[0] if e.args else None
                if eno not in EAGAIN_ERRNOS:
                    raise
                tries += 1
                if tries > 200:
                    raise
                try:
                    time.sleep_ms(1)
                except Exception:
                    try:
                        time.sleep(0.001)
                    except Exception:
                        pass
        return sent

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _http_req_complete(buf):
    """请求是否读全：head 结束标志 + content-length 对应 body 已到齐。"""
    i = buf.find(b"\r\n\r\n")
    if i < 0:
        return False
    clen = 0
    for line in buf[:i].split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                clen = int(line.split(b":", 1)[1].strip())
            except Exception:
                clen = 0
            break
    return len(buf) - (i + 4) >= clen


def _http_abort():
    """丢弃当前未完成的连接（半开 / 超时 / 超长）。"""
    global _http_conn, _http_buf, _http_aborts
    conn = _http_conn
    _http_conn = None
    _http_buf = b""
    if conn is None:
        return
    _http_aborts += 1
    try:
        conn.close()
    except Exception:
        pass


def stop_control_http():
    """关闭 HTTP 监听 socket 与未完成连接（portal 切换前调用）。"""
    global _http_srv, _http_stop
    _http_stop = True
    _http_abort()
    srv = _http_srv
    _http_srv = None
    if srv is not None:
        try:
            srv.close()
        except Exception:
            pass
        print("[HTTP API] stopped")


def http_poll(cfg):
    """主循环调用的非阻塞 HTTP 服务（v6.0.5）。

    设计要点（修复「/api/relay 并发 >=4 整机冻结」）：
      - 所有 socket 操作都在主线程，不再与主循环的其他 lwIP 调用跨线程竞争；
      - accept/recv 全非阻塞：单轮最多处理 HTTP_MAX_CONN_PER_POLL 个「已就绪」
        连接，遇到没有新数据/队列为空就立刻收手，绝不阻塞主循环（MQTT 心跳、
        Modbus 采集节奏不受影响）；
      - 单连接状态机 + 3s 读超时 + 8KB 上限：慢连接/半开连接会被丢弃，
        不会拖死监听队列或内存。
    """
    global _http_conn, _http_buf, _http_deadline_ms
    global _http_polls, _http_reqs
    _http_polls += 1
    srv = _http_srv
    if srv is None or _http_stop:
        return

    for _round in range(HTTP_MAX_CONN_PER_POLL):
        now = now_ms()

        if _http_conn is None:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return                   # EAGAIN：监听队列为空，本轮结束
            except Exception:
                return
            try:
                conn.setblocking(False)
            except Exception:
                pass
            _http_conn = conn
            _http_buf = b""
            _http_deadline_ms = time.ticks_add(now, HTTP_FIRST_BYTE_TIMEOUT_MS)

        conn = _http_conn
        try:
            d = conn.recv(HTTP_RECV_CHUNK)
        except OSError as e:
            eno = e.args[0] if e.args else None
            if eno in EAGAIN_ERRNOS:
                d = None                 # 数据未到
            else:
                _http_abort()            # 连接已坏（RST 等）
                continue
        except Exception:
            _http_abort()
            continue

        if d:
            if not _http_buf:
                # 首字节到了：把「首字节超时」换成完整请求的读超时
                _http_deadline_ms = time.ticks_add(now, HTTP_READ_TIMEOUT_MS)
            _http_buf += d

        if d is None:
            # 当前连接数据还没到：可能只是慢，收手等下一轮（不忙等）
            if time.ticks_diff(now, _http_deadline_ms) >= 0:
                _http_abort()            # 首字节超时 / 读超时
            return
        if d == b"" and not _http_req_complete(_http_buf):
            _http_abort()                # 对端提前关闭且请求不全
            continue

        if not _http_req_complete(_http_buf):
            if (len(_http_buf) > HTTP_MAX_REQ_BYTES
                    or time.ticks_diff(now, _http_deadline_ms) >= 0):
                _http_abort()
                continue
            return                       # 还需要更多数据，本轮结束

        # 请求读全 -> 交路由处理；无论成败都关闭连接
        view = _HttpPrebufferedConn(conn, _http_buf)
        _http_conn = None
        _http_buf = b""
        _http_reqs += 1
        try:
            http_api_handler(cfg, view)
        except Exception as e:
            print("[HTTP API] handler error:", e)
            try:
                http_send(view, "500 Internal Server Error", "err: %s" % e)
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # 达到本轮配额，剩余连接留到下一轮（保证主循环不会被 HTTP 独占）


def start_control_http(cfg):
    """在 STA 模式下启动 HTTP 控制 API（端口 80），用于局域网直连控制继电器。

    v6.0.5：**不再创建独立线程**。改为只创建「非阻塞监听 socket」，由主循环
    每轮调 http_poll() 串行处理连接（原因见上方 HTTP_* 常量处的详细说明）。

    仍保留 _thread.stack_size(4*1024)：MicroPython 里 stack_size 是全局设置，
    对之后创建的所有线程生效；紧随其后的 Modbus worker 线程仍依赖它。
    （C3 实测：8192/6144 均 can't create thread，4096 是上限。）
    """
    global _http_srv, _http_stop
    if _thread is not None:
        try:
            _thread.stack_size(4 * 1024)
        except Exception:
            pass
    _http_stop = False
    _http_srv = None
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", 80))
        srv.listen(HTTP_LISTEN_BACKLOG)
        srv.setblocking(False)
        _http_srv = srv
        print("[HTTP API] listening on port 80 (main-loop polled, non-blocking)")
    except Exception as e:
        _http_srv = None
        print("[HTTP API] bind error:", e)


def portal(cfg):
    """进入 AP 配网模式

    必须在无其他线程持有网络资源时切换，否则 STA/AP 切换会触发
    Guru Meditation (Load access fault) 崩溃重启、热点一闪而逝。
    顺序：停 HTTP -> 断 MQTT -> 全关继电器 -> 停 Modbus -> 关 STA -> 开 AP。
    """
    global client
    # 0) WiFi 失败风暴守卫（须在一切 esp_wifi/lwIP 操作之前，只用纯 Python 标志
    #    判断，不调 status()/isconnected()）：esp-sha 风暴后驱动进入坏状态，任何
    #    esp_wifi 模式切换都会撞同址 Load access fault。machine.reset() 直接
    #    esp_restart、不经 esp_wifi_stop，是唯一不碰驱动的重启；故风暴 -> 复位
    #    重试循环。无配置(首配)时从未真正连过，不算失败，正常开 AP。见 ROOTCAUSES 2。
    if cfg.get("wifi_ssid") and _wifi_failed_this_boot and not _sta_connected_once:
        print("[portal] wifi fail storm this boot -> soft reset retry (streak=%d)" % _wifi_fail_streak)
        time.sleep(1)
        reset()
        return
    # 1) 关闭 HTTP 监听 socket 释放端口 80。v6.0.5 起 HTTP 在主循环轮询，没有
    #    独立线程要等退出，主线程关掉监听即释放（SO_REUSEADDR 防 TIME_WAIT）。
    stop_control_http()
    # 2) 断开 MQTT（幂等）
    mqtt_disconnect()
    # 3) 上电安全：继电器全部断开
    set_all_relay(False)
    # 4) 停止 Modbus 采集线程
    stop_modbus()
    # 5) 断开并关闭 STA。只在本进程曾 active(True) 过 STA 时才触碰 esp_wifi，
    #    且只在当前已连接时 teardown —— 未启动过的接口 / 失败态滞留 1001/39 时
    #    调 disconnect()/active(False) 会 Load access fault 硬崩溃（拦不住）。
    if _sta_was_active:
        _teardown = False
        try:
            _teardown = bool(wlan_sta.isconnected())
        except Exception:
            _teardown = False
        if _teardown:
            try:
                wlan_sta.disconnect()
            except Exception:
                pass
            time.sleep(0.3)
            try:
                wlan_sta.active(False)
            except Exception:
                pass
        else:
            print("[portal] STA not connected, skip STA teardown")
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password="", authmode=network.AUTH_OPEN)
    try:
        ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
    except Exception:
        pass
    print("配网模式: 连接热点 %s，打开 http://%s" % (AP_SSID, AP_IP))
    # 等 STA HTTP server 线程释放 80 端口；之前直接 bind 会撞 EADDRINUSE 致命错
    time.sleep(1)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound = False
    for attempt in range(5):
        try:
            srv.bind(("0.0.0.0", 80))
            bound = True
            break
        except OSError as e:
            print("portal bind retry %d: %s" % (attempt + 1, e))
            time.sleep(1)
    if not bound:
        print("portal bind failed after retries")
        time.sleep(1)
        reset()
    srv.listen(2)
    while True:
        try:
            conn, addr = srv.accept()
        except Exception:
            continue
        try:
            conn.settimeout(3)
            req = conn.recv(2048)
            head, _, rest = req.partition(b"\r\n\r\n")
            first = head.split(b"\r\n", 1)[0].decode("utf-8", "replace").split()
            method, path = first[0], first[1]
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":")[1])
            while len(rest) < clen:
                rest += conn.recv(1024)
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            continue
        if method == "GET" and path == "/":
            try:
                body = render_page(cfg)
                http_send(conn, "200 OK", body)
            except Exception as e:
                print("render_page err:", e)
                try:
                    http_send(conn, "500 Internal Server Error", "render error: %s" % e)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
        elif method == "POST" and path == "/save":
            form = parse_form(rest.decode("utf-8", "replace"))
            for k in ("wifi_ssid", "wifi_password", "mqtt_host", "mqtt_user", "mqtt_password", "product_id", "device_id"):
                if k in form:
                    cfg[k] = form[k]
            cfg["mqtt_port"] = int(form.get("mqtt_port") or 1883)
            cfg["report_interval"] = int(form.get("report_interval") or REPORT_INTERVAL_S)
            cfg["topic_mode"] = form.get("topic_mode", "direct")
            # 解析 Modbus JSON 配置
            modbus_text = form.get("modbus_json", "")
            if modbus_text:
                try:
                    mb_cfg = json.loads(modbus_text)
                    if isinstance(mb_cfg, dict):
                        cfg["modbus"] = mb_cfg
                        print("[MAIN] parsed modbus config, slaves:", len(mb_cfg.get("slaves", [])))
                    else:
                        print("[MAIN] modbus_json not a dict, ignored")
                except Exception as e:
                    print("[MAIN] modbus_json parse error:", e)
            save_config(cfg)
            http_send(conn, "200 OK", "已保存，设备将在 1 秒后重启并联网。若 30 秒连不上 WiFi 会重新进入配网热点。")
            time.sleep(1)
            reset()
        elif method == "GET" and path == "/api/modbus_values":
            # 配网模式下 Modbus 线程未启动，返回空值即可
            body = json.dumps({"ok": True, "values": {}})
            http_send(conn, "200 OK", body, "application/json")
        else:
            http_send(conn, "404 Not Found", "404")


# -------------------- WiFi / 正常模式 --------------------
def connect_wifi(cfg):
    """连接 STA WiFi（状态机版）。

    关键经验（v5.1/v6.0.1 实机踩坑）：
    1. connect() 后驱动进入 connecting(1001)，此期间再调 connect() 会报
       "sta is connecting, return error / Wifi Internal Error" —— 这是正常
       拒绝，不是故障，绝不能因此触发 active(False) 重启接口（在连接中
       deinit WLAN 会 Guru Meditation 崩溃，热点/日志循环重启）。
    2. 驱动空闲(IDLE=1000)或处于失败态时才发起 connect；connecting(1001)
       时只等待。
    3. 收到失败码(201 无AP/202 密码错/203 连接失败)时 disconnect 重置，
       然后**不等驱动回 1000 直接重发**——实测 disconnect() 后 status 可能
       长期停留在失败码不回 IDLE，若傻等会陷入 203 无限重试死循环。
    """
    import gc
    global _sta_was_active, _sta_connected_once, _wifi_fail_streak, _wifi_failed_this_boot
    wlan_sta.active(True)
    _sta_was_active = True  # 记录本进程已启动过 STA（portal 收尾据此决定是否 teardown）
    time.sleep(2)  # 等驱动就绪，避免 Internal Error
    gc.collect()  # 回收碎片后再 connect，降低握手期内存压力
    start = now_ms()
    issued = False  # 本次循环是否已发出 connect
    tries = 0
    _fail_seen = None  # 失败码连续出现起点（esp-sha 自愈观察窗计时）
    FAIL_SETTLE_MS = 12000  # 失败码静默观察窗：期间不断开，给驱动自愈留窗口
    while not wlan_sta.isconnected():
        _now2 = now_ms()
        # === 驱动自愈观察窗（esp-sha 瞬时内存压力）===
        # 收到失败码后先静默观察 FAIL_SETTLE_MS：驱动内部会自行重试握手
        # （esp-sha buffer 分配失败可刷屏数十秒后最终连上），前提是不 disconnect
        # 打断它。期间只轮询 isconnected，窗口耗尽仍未连上才允许重置。
        if _fail_seen is not None and \
                time.ticks_diff(_now2, _fail_seen) < FAIL_SETTLE_MS:
            for _ in range(6):
                if wlan_sta.isconnected():
                    break
                time.sleep(0.5)
            continue
        if tries >= 8:
            print("[wifi] give up after 8 tries, enter portal")
            _wifi_fail_streak += 1
            _wifi_failed_this_boot = True
            # 防御性收尾：确保驱动不在 connecting 就进 portal（避免 teardown 崩溃）
            try:
                if not wlan_sta.isconnected() and wlan_sta.status() != 1000:
                    wlan_sta.disconnect()
            except Exception:
                pass
            time.sleep(0.3)
            return False
        if time.ticks_diff(_now2, start) > WIFI_TIMEOUT_S * 1000:
            try:
                print("[wifi] TIMEOUT status=%d active=%s connected=%s" % (wlan_sta.status(), wlan_sta.active(), wlan_sta.isconnected()))
            except Exception as _e:
                print("[wifi] TIMEOUT status print err:", _e)
            print("WiFi connect timeout")
            _wifi_fail_streak += 1
            _wifi_failed_this_boot = True
            # 超时返回前把驱动收尾到空闲：若仍卡在 connecting(1001) 就直接进
            # portal()，那里对"连接中"的 STA 做 disconnect/active(False) 会硬崩溃。
            # 先等最多 8s 落定——若迟到连上则返回成功，否则 disconnect 中止。
            _settle = now_ms()
            while not wlan_sta.isconnected() and wlan_sta.status() == 1001 \
                    and time.ticks_diff(now_ms(), _settle) < 8000:
                time.sleep(0.5)
            if wlan_sta.isconnected():
                print("WiFi connected:", wlan_sta.ifconfig()[0])
                _sta_connected_once = True
                _wifi_fail_streak = 0
                _wifi_failed_this_boot = False
                return True
            # 已离开 connecting（落定为失败码/空闲）才 disconnect 收尾；若 8s 后
            # 仍死死卡在 connecting，则原样返回（portal 侧的守卫也会跳过 teardown，
            # AP 与卡住的 STA 并发即可），绝不触碰连接中的驱动。
            try:
                if wlan_sta.status() != 1001:
                    wlan_sta.disconnect()
            except Exception:
                pass
            time.sleep(0.3)
            gc.collect()
            return False
        st = wlan_sta.status()
        if st == 1000 and not issued:
            # 驱动空闲 -> 发起连接（只发一次，之后交给轮询）
            tries += 1
            try:
                wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
                issued = True
                print("[wifi] connect issued try#%d" % tries)
            except Exception as e:
                # 偶发 "sta is connecting" 忽略：驱动实际已在连接
                print("[wifi] connect issue err (ignore):", e)
                issued = True
        elif st in (201, 202, 203, 1002, 1003, 1004):
            # MicroPython ESP32 失败码：201=无AP 202=密码错/认证失败 203=连接失败
            # （1002/1003/1004 为兼容保留）。
            if not issued:
                # 已 disconnect 过但 status 仍滞留失败码（驱动不回 IDLE 1000）：
                # 直接重发 connect，不能傻等 1000 —— 否则 203 无限重试死循环。
                tries += 1
                try:
                    wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
                    issued = True
                    print("[wifi] reconnect on stale fail st=%d try#%d" % (st, tries))
                except Exception as e:
                    print("[wifi] reconnect issue err (ignore):", e)
                    issued = True
                gc.collect()
            else:
                # 已发出连接且收到失败码。esp-sha 瞬时内存压力下驱动会自行
                # 重试握手（while 顶部观察窗）；首次见失败码只记录起点交给
                # 观察窗静默等待，窗口耗尽仍停留失败码才 disconnect 重置。
                if _fail_seen is None:
                    _fail_seen = now_ms()
                    print("[wifi] fail code", st,
                          "- enter %.1fs settle window" % (FAIL_SETTLE_MS / 1000))
                else:
                    print("[wifi] fail code", st,
                          "persisted >%.1fs - disconnect+retry try#%d"
                          % (FAIL_SETTLE_MS / 1000, tries + 1))
                    try:
                        wlan_sta.disconnect()
                    except Exception:
                        pass
                    time.sleep(4)
                    issued = False
                    _fail_seen = None
                    start = now_ms()  # 重置完整超时预算，给每次尝试完整窗口
                gc.collect()
        # 短轮询等待
        for _ in range(6):
            if wlan_sta.isconnected():
                break
            time.sleep(0.5)
    print("WiFi connected:", wlan_sta.ifconfig()[0])
    _sta_connected_once = True
    _wifi_fail_streak = 0
    _wifi_failed_this_boot = False
    return True


def led_set(on):
    if led is not None:
        led.value(0 if on else 1)


def start_modbus(cfg):
    """连上 WiFi 后启动 Modbus 采集线程（TCP/RTU 二选一，独立线程不阻塞继电器主循环）"""
    global mb_master, ModbusMaster, ModbusTCPMaster, HAS_MODBUS, HAS_MODBUS_TCP
    mb_cfg = cfg.get("modbus") or DEFAULT_MODBUS_CONFIG
    if not mb_cfg.get("enabled", True):
        print("[MAIN] Modbus disabled in config")
        return False
    mode = mb_cfg.get("mode", "tcp")
    # 如果已经启动过则先停止，再重新初始化（配置可能已变）
    if mb_master is not None:
        try:
            mb_master.stop()
        except Exception as e:
            print("[MAIN] stop old modbus master error:", e)
        mb_master = None
    try:
        if mode == "tcp":
            if not HAS_MODBUS_TCP or ModbusTCPMaster is None:
                # 延迟 import：只在需要时加载 TCP 模块，省堆给线程栈
                try:
                    from modbus_tcp_master import ModbusTCPMaster
                    HAS_MODBUS_TCP = True
                except Exception as _e:
                    HAS_MODBUS_TCP = False
                    print("[MAIN] modbus_tcp_master import failed:", _e)
            if not HAS_MODBUS_TCP or ModbusTCPMaster is None:
                print("[MAIN] ModbusTCPMaster module not available, skip")
                return False
            mb_master = ModbusTCPMaster(mb_cfg)
        else:
            if not HAS_MODBUS or ModbusMaster is None:
                # 延迟 import：RTU 模式才加载串口版模块
                try:
                    from modbus_master import ModbusMaster
                    HAS_MODBUS = True
                except Exception as _e:
                    HAS_MODBUS = False
                    print("[MAIN] modbus_master import failed:", _e)
            if not HAS_MODBUS or ModbusMaster is None:
                print("[MAIN] ModbusMaster (RTU) module not available, skip")
                return False
            mb_master = ModbusMaster(mb_cfg)
        ok = mb_master.init_hw() and mb_master.start()
        print("[MAIN] start_modbus mode=%s result:" % mode, ok)
        return ok
    except Exception as e:
        print("[MAIN] start_modbus exception:", e)
        mb_master = None
        return False


def stop_modbus():
    global mb_master
    if mb_master is not None:
        try:
            mb_master.stop()
        except Exception as e:
            print("[MAIN] stop_modbus error:", e)
        mb_master = None


def wifi_reassoc_if_dead(cfg, now):
    """v6.0.5 自愈 D：WiFi 自称已连接但数据面已死时，主动重新关联。

    只在「isconnected() 为 True」时介入 —— 真断线走主循环原有的重连分支。
    返回 True 表示本次触发了重新关联。
    """
    global mqtt_fail_streak, wifi_reassocs, wifi_reassoc_ms, mqtt_retry
    if mqtt_fail_streak < MQTT_FAIL_REASSOC:
        return False
    # v6.0.5 修正：初值必须是 None 而非 0。0 会被当成真实时间戳，
    # ticks_diff(now,0) 在开机 90s 内恒 < 冷却值 -> 首次自愈被误挡（实测
    # streak 累积到 9 才触发，阈值只有 3）。
    if wifi_reassoc_ms is not None and \
            time.ticks_diff(now, wifi_reassoc_ms) < WIFI_REASSOC_COOLDOWN_MS:
        return False
    try:
        if not wlan_sta.isconnected():
            return False        # 真断线：交给原有 WiFi 重连分支，这里不插手
        st = wlan_sta.status()
    except Exception as e:
        print("[wifi] reassoc status err:", e)
        return False

    print("[wifi] data path dead: %d consecutive MQTT connect failures, "
          "isconnected=True status=%s -> re-associate" % (mqtt_fail_streak, st))
    try:
        wlan_sta.disconnect()
    except Exception as e:
        print("[wifi] reassoc disconnect err:", e)
    time.sleep(2)               # 等驱动回到可发起状态
    # 驱动偶尔仍处于 "sta is connecting" 收尾窗口，直接 connect 会被拒。
    # 这里重试两次（间隔 1s），比丢给 90s 冷却再等一轮强得多。
    for _try in (1, 2):
        try:
            wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
            break
        except Exception as e:
            print("[wifi] reassoc connect err (try%d):" % _try, e)
            time.sleep(1)
    wifi_reassocs += 1
    wifi_reassoc_ms = now_ms()
    mqtt_fail_streak = 0
    mqtt_retry = 0              # 立刻重试 MQTT，尽快验证链路是否真的活了
    return True


def net_dead_track(now, mqtt_ok):
    """维护「MQTT 连续不可用」时长（秒），供自愈 E 复位判据使用。

    mqtt_ok=True 表示此刻 MQTT 可用（client 非 None）。用「MQTT 能否连上」这一个
    信号统一表达"网络可用吗"——同时覆盖 isconnected() 真但数据面假死、以及
    isconnected() 为假两种情形。
    """
    global net_dead_since, net_dead_base_reassocs
    if mqtt_ok:
        net_dead_since = 0
        return 0
    if not _sta_connected_once:
        return 0                 # 从未连上过：冷启动问题，交给 portal 的失败风暴守卫
    if net_dead_since == 0:
        net_dead_since = now
        net_dead_base_reassocs = wifi_reassocs   # 记下基线：本轮捅了几次从零算
    return time.ticks_diff(now, net_dead_since) // 1000


def net_reset_if_hopeless(cfg, now):
    """自愈 E：网络彻底不可用（捅了几次都没救回来）-> 硬复位。

    返回 True 表示已发起复位（调用方应立刻 continue）。machine.reset() 不走
    esp_wifi_stop，复位后 boot.py/wifi_boot.py 在空堆重新预连（v6.0.1 实证最稳）。
    """
    global net_resets
    if not _sta_connected_once or net_dead_since == 0:
        return False
    dead_s = time.ticks_diff(now, net_dead_since) // 1000
    if dead_s * 1000 < NET_RESET_AFTER_MS:
        return False
    if wifi_reassocs - net_dead_base_reassocs < NET_RESET_MIN_REASSOCS:
        return False
    print("[net] unusable %ds (reassocs_this_round=%d mqtt_fails=%d) "
          "-> HARD RESET to boot pre-connect"
          % (dead_s, wifi_reassocs - net_dead_base_reassocs, mqtt_fail_streak))
    net_resets += 1
    time.sleep(0.3)
    reset()
    return True


def run_normal(cfg):
    global client, last_ping, last_report, mqtt_retry, wifi_retry, long_triggered, portal_requested, short_requested, sw_short_requested, mb_master, _http_stop
    global pub_fail_streak, hb_timeout_active_s, force_reconnect, report_requested
    global stall_reconnect_active_s
    if not connect_wifi(cfg):
        return False

    # v6.0.5：先建 HTTP 非阻塞监听 socket（主循环轮询，不再开线程），再启
    # Modbus worker。start_control_http 里仍会设 _thread.stack_size(4KB)，
    # 好让随后的 Modbus 线程能在 C3 的内部 RAM 上限内创建成功。
    start_control_http(cfg)
    # Modbus 采集线程（TCP/RTU 二选一，独立线程不阻塞继电器主循环）
    start_modbus(cfg)

    interval_ms = max(1, int(cfg.get("report_interval", REPORT_INTERVAL_S))) * 1000
    last_report = 0
    last_ping = now_ms()
    mqtt_retry = 0
    wifi_retry = now_ms()
    # v6.0.4 心跳超时阈值（可用板子 config.json 的 hb_timeout_s 覆盖）
    hb_timeout_s = int(cfg.get("hb_timeout_s", HEARTBEAT_TIMEOUT_S))
    hb_timeout_ms = max(1, hb_timeout_s) * 1000
    hb_timeout_active_s = hb_timeout_s
    # v6.0.4 bridge 报告的停滞阈值（可用 board config.json 的 stall_reconnect_s 覆盖）
    stall_reconnect_active_s = max(10, int(cfg.get("stall_reconnect_s", STALL_RECONNECT_S)))

    if not mqtt_connect(cfg):
        pass  # 下面循环会继续重试

    while True:
        now = now_ms()

        # v6.0.5：HTTP 控制 API 在主循环里非阻塞轮询（取代原独立线程）。
        # 放在循环最顶部：无论走哪个 continue 分支（WiFi/MQTT 断开等），
        # 每轮都会给 HTTP 一次机会，保证并发下请求不会饿死。
        http_poll(cfg)

        # 检查 SW1 / SW 按键
        handle_button(cfg)
        handle_sw_button(cfg)
        # 消费短按请求：HTTP/GPIO 短按都通过 flag，主循环内调 publish 安全
        if short_requested:
            short_requested = False
            print("[BUTTON] consuming SW1 short press request")
            try:
                publish_property(cfg)
                print("[BUTTON] SW1 short press publish OK")
            except Exception as e:
                print("[BUTTON] SW1 short press publish err:", e)
        if sw_short_requested:
            sw_short_requested = False
            print("[SW] consuming SW short press publish request")
            try:
                publish_property(cfg)
                print("[SW] SW short press publish OK")
            except Exception as e:
                print("[SW] SW short press publish err:", e)
        if report_requested:
            # v6.0.4：HTTP /api/relay 触发的补报（当时 HTTP 在独立线程，只置标志
            # 不碰 socket）。v6.0.5 HTTP 已回到主循环，这条统一上报路径保留不变。
            report_requested = False
            try:
                publish_property(cfg)
                print("[HTTP] relay change report published")
            except Exception as e:
                print("[HTTP] relay change report err:", e)
        if long_triggered or portal_requested:
            long_triggered = False
            portal_requested = False
            print("SW1 long press -> portal (arm soft-reset flag)")
            # STA 热切进 AP 时多线程持有网络资源，与主线程并发切换 WiFi 会
            # lwIP Load access fault 崩溃（热点一闪而逝）。故写一次性标志文件
            # 后软复位，上电在无线程的干净环境直接进 portal。
            _http_stop = True  # 尽力而为：复位后自然消失（真正释放靠 machine.reset）
            try:
                with open(PORTAL_FLAG, "w") as _f:
                    _f.write("1")
            except Exception as _e:
                print("[BUTTON] write portal flag err:", _e)
            time.sleep(0.3)
            reset()

        # v6.0.5 自愈 E：跟踪「MQTT 连续不可用」时长，捅几次还不行就硬复位。放这里
        # （WiFi 分支之前）是刻意的：无论 isconnected() 真假每轮都评估一次 ——
        # 「自愈 D 捅完停在 IDLE」的死局走的是下面的 WiFi 分支，到不了 MQTT 分支。
        _net_dead_s = net_dead_track(now, client is not None)
        if _net_dead_s:
            if net_reset_if_hopeless(cfg, now):
                continue

        # WiFi 断线重连
        if not wlan_sta.isconnected():
            led_set(False)
            if time.ticks_diff(now, wifi_retry) >= 0:
                wifi_retry = time.ticks_add(now, RETRY_S * 1000)
                print("WiFi lost, retrying...")
                try:
                    wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
                except Exception as e:
                    # "sta is connecting" 是驱动连接中的正常拒绝，下周期再试；
                    # 其它错误别无声吞掉——它是排查"卡在 IDLE"的唯一线索。
                    print("WiFi retry err:", e)
            time.sleep(0.2)
            continue

        # MQTT 断线重连 / 维护
        if client is None:
            led_set(False)
            if time.ticks_diff(now, mqtt_retry) >= 0:
                mqtt_retry = time.ticks_add(now, RETRY_S * 1000)
                print("MQTT retry...")
                mqtt_connect(cfg)
                # v6.0.5 自愈 D：建连仍失败 + WiFi 自称已连接 -> 数据面可能假死。
                # 这是「isconnected() 为真但收发包全丢」的唯一出口，没有它板子
                # 会永远卡在 MQTT 重试里（实板症状：ECONNABORTED 死循环）。
                if client is None:
                    wifi_reassoc_if_dead(cfg, now)
        else:
            try:
                client.check_msg()
                # v6.0.4 自愈 C：bridge 通过心跳回传「父设备属性已停滞 N 秒」
                # -> 说明板子上行被静默丢弃（板子自己察觉不到），主动断开重连。
                if force_reconnect:
                    force_reconnect = False
                    print("[MQTT] reconnect requested by gateway -> force reconnect")
                    mqtt_disconnect()
                    mqtt_retry = now
                    continue
                # v6.0.4 自愈 A：连续 publish 失败 -> 写通道已坏，主动断开重连
                if pub_fail_streak >= PUB_FAIL_LIMIT:
                    print("[MQTT] %d consecutive publish failures -> force reconnect"
                          % pub_fail_streak)
                    pub_fail_streak = 0
                    mqtt_disconnect()
                    mqtt_retry = now
                    continue
                # v6.0.4 自愈 B：网关心跳超时 -> 判定 MQTT 半死（TCP 看似
                # ESTABLISHED、publish 静默丢失），主动断开重连。
                # 没有这条时，板子会一直"假装在线"，直到人为干预。
                if time.ticks_diff(now, last_hb_ms) >= hb_timeout_ms:
                    print("[MQTT] gateway heartbeat timeout(%ds) -> force reconnect"
                          % hb_timeout_s)
                    mqtt_disconnect()
                    mqtt_retry = now
                    continue
                if time.ticks_diff(now, last_ping) >= MQTT_PING_S * 1000:
                    client.ping()
                    last_ping = now
                if time.ticks_diff(now, last_report) >= interval_ms:
                    publish_property(cfg)
                    last_report = now
                led_set(True)
            except Exception as e:
                print("MQTT error:", e)
                client = None
                mqtt_retry = time.ticks_add(now, RETRY_S * 1000)
                led_set(False)

        # v6.0.5：HTTP 是「单连接状态机」，而 http_poll() 遇到「数据未到」会提前
        # return —— 于是每个连接至少要跨 2 轮主循环。若仍按 0.1s 睡，吞吐会被压到
        # ~5 连接/秒：实板 n=32 时监听队列持续溢出，客户端 SYN 重传直到 12s 超时。
        # 有连接在途时把间隔压到 20ms（无连接时保持 0.1s，不额外费电）。
        time.sleep(0.02 if _http_conn is not None else 0.1)


# -------------------- 按键处理 --------------------
def trigger_short_press(cfg, source="GPIO"):
    """短按触发：仅设置标志 short_requested，主循环下次 tick 统一 publish。

    避免 HTTP 线程直接调 publish_property 和主循环抢 MQTT socket 引发 WiFi 崩溃。
    """
    global short_requested
    short_requested = True
    print("[BUTTON] short press via %s, queued for main loop to publish" % source)
    return True, None


def trigger_long_press(cfg, source="GPIO", enter_portal=False):
    """长按触发：默认仅模拟（不真正进 AP），加 enter_portal=True 才会踢出 STA 模式

    HTTP 默认 enter_portal=False 避免裸测时被一脚踢出 broker 会话；
    真要测 portal 转换，传 enter_portal=True 或 HTTP query 里加 real=1。
    """
    global portal_requested
    if enter_portal:
        portal_requested = True
        print("[BUTTON] long press via %s, portal transition armed" % source)
    else:
        print("[BUTTON] long press via %s, simulated (no portal transition)" % source)
    return True


def handle_button(cfg):
    """SW1 按键（仅 GPIO 路径，HTTP 走 trigger_short_press/trigger_long_press）：
    - 短按（按下后 < LONG_PRESS_MS 释放） → trigger_short_press()（设 flag 由主循环 publish）
    - 长按（持续 >= LONG_PRESS_MS） → 设 long_triggered=True，run_normal 进入 portal

    当 button 引脚未配置（SW1_PIN=None）时，整个函数退化为 no-op；
    HTTP 路径不受影响，仍可触发按钮语义。
    """
    global button_state, press_start, long_triggered, short_triggered
    if button is None:
        return
    st = button.value()
    now = now_ms()
    if st == 0 and button_state == 1:
        # 刚按下
        press_start = now
        long_triggered = False
        short_triggered = False
    if st == 0 and not long_triggered:
        if time.ticks_diff(now, press_start) >= LONG_PRESS_MS:
            long_triggered = True
            # 用 trigger_long_press 让 GPIO 和 HTTP 走同一路径；enter_portal=True 走真的
            trigger_long_press(cfg, source="GPIO", enter_portal=True)
            print("[BUTTON] GPIO long press armed, run_normal will enter portal")
    if st == 1 and button_state == 0:
        # 刚释放
        dur = time.ticks_diff(now, press_start)
        if not long_triggered and not short_triggered and 0 < dur < LONG_PRESS_MS:
            short_triggered = True
            trigger_short_press(cfg, source="GPIO")
    button_state = st


def trigger_sw_short(cfg, source="GPIO"):
    """SW（IO9）短按：循环切换下一路继电器状态（CH1→CH2→CH3→CH4→全关→循环）

    触发后立即 publish 当前继电器状态（通过 sw_short_requested flag，主循环消费）
    GPIO 和 HTTP 两条路径都复用本函数。
    """
    global sw_cycle_idx, sw_short_requested
    target = SW_CYCLE[sw_cycle_idx]
    sw_cycle_idx = (sw_cycle_idx + 1) % len(SW_CYCLE)
    if target == "all":
        # 循环里的"全关"步骤
        changed = set_all_relay(False)
        print("[SW] short press via %s, all relays OFF (cycle %d/%d) changed=%s"
              % (source, sw_cycle_idx, len(SW_CYCLE), changed))
    else:
        cur = relay_is_on(target)
        set_relay(target, not cur)
        print("[SW] short press via %s, CH%d -> %s (cycle %d/%d)"
              % (source, target + 1, "ON" if not cur else "OFF", sw_cycle_idx, len(SW_CYCLE)))
    sw_short_requested = True  # 主循环消费后立即 publish
    return True, None


def trigger_sw_long(cfg, source="GPIO"):
    """SW（IO9）长按：所有继电器强制关闭（应急复位）"""
    changed = set_all_relay(False)
    print("[SW] long press via %s, all relays OFF (emergency reset) changed=%s"
          % (source, changed))
    # 同时把 cycle 回到 0，让下次短按从 CH1 开始
    global sw_cycle_idx
    sw_cycle_idx = 0
    # 长按也 publish 一次当前状态
    global sw_short_requested
    sw_short_requested = True
    return True, None


def handle_sw_button(cfg):
    """SW（IO9，按下时机不同）GPIO 路径：
    - 短按（< SW_LONG_PRESS_MS 释放） → trigger_sw_short()
    - 长按（>= SW_LONG_PRESS_MS） → trigger_sw_long()
    当 sw_button 引脚未配置时退化为 no-op；HTTP 路径不受影响。
    """
    global sw_button_state, sw_press_start, sw_long_triggered, sw_short_triggered
    if sw_button is None:
        return
    st = sw_button.value()
    now = now_ms()
    if st == 0 and sw_button_state == 1:
        # 刚按下
        sw_press_start = now
        sw_long_triggered = False
        sw_short_triggered = False
    if st == 0 and not sw_long_triggered:
        if time.ticks_diff(now, sw_press_start) >= SW_LONG_PRESS_MS:
            sw_long_triggered = True
            trigger_sw_long(cfg, source="GPIO")
    if st == 1 and sw_button_state == 0:
        # 刚释放
        dur = time.ticks_diff(now, sw_press_start)
        if not sw_long_triggered and not sw_short_triggered and 0 < dur < SW_LONG_PRESS_MS:
            sw_short_triggered = True
            trigger_sw_short(cfg, source="GPIO")
    sw_button_state = st


# -------------------- 主入口 --------------------
def main():
    global wlan_sta, wlan_ap, button, sw_button, led
    cfg = load_config()
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_ap = network.WLAN(network.AP_IF)

    # 初始化硬件
    init_relays(cfg.get("relay_pins"))
    sw1 = cfg.get("sw1_pin", SW1_PIN)
    # sw1 可能为 None（无杜邦线 / 板载按钮与此固件冲突），禁用 GPIO 短按路径
    global button
    if sw1 is None:
        button = None
        print("[MAIN] SW1_PIN disabled, GPIO SW1 inactive (HTTP only)")
    else:
        button = Pin(int(sw1), Pin.IN, Pin.PULL_UP)
        print("[MAIN] SW1_PIN = GPIO%d" % int(sw1))
    # SW (IO9) 按钮：板上 BOOT 按钮同脚；运行时为普通 GPIO
    sw = cfg.get("sw_pin", SW_PIN)
    global sw_button
    if sw is None:
        sw_button = None
        print("[MAIN] SW_PIN disabled, GPIO SW inactive (HTTP only)")
    else:
        sw_button = Pin(int(sw), Pin.IN, Pin.PULL_UP)
        print("[MAIN] SW_PIN = GPIO%d (short=cycle, long=all-off)" % int(sw))
    if LED_PIN is not None:
        led = Pin(LED_PIN, Pin.OUT, value=1)  # 熄灭（假设低电平亮）

    # 上电安全：继电器全部断开
    set_all_relay(False)

    # portal 软复位标志存在（按键/HTTP 请求过配网）-> 直接进 AP 配网。
    # 此时尚未启任何线程，干净环境进 portal 不会触发 lwIP 崩溃。
    try:
        os.stat(PORTAL_FLAG)
        os.remove(PORTAL_FLAG)
        print("[MAIN] portal flag found, enter AP portal (clean env)")
        stop_modbus()
        portal(cfg)
        return
    except OSError:
        pass

    # 无配置 / 连不上 WiFi -> 进入配网
    if not cfg.get("wifi_ssid"):
        print("no wifi config, enter portal")
        stop_modbus()
        portal(cfg)
        return

    while True:
        ok = run_normal(cfg)
        if not ok:
            print("[MAIN] run_normal returned False -> portal fallback")
            stop_modbus()
            portal(cfg)
            return


try:
    main()
except Exception as e:
    print("FATAL:", e)
    import sys
    sys.print_exception(e)
    time.sleep(3)
    reset()
