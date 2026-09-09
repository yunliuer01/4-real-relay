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
"""
import json
import os
import network
import socket
import time
import machine
from machine import Pin, reset

# 如果 modbus 模块已经上传到板子，则导入；否则给出占位对象，避免 main.py 直接崩溃
try:
    from modbus_tcp_master import ModbusTCPMaster
    HAS_MODBUS_TCP = True
except Exception as _e:
    print("[MAIN] modbus_tcp_master import failed:", _e)
    ModbusTCPMaster = None
    HAS_MODBUS_TCP = False

try:
    from modbus_master import ModbusMaster
    HAS_MODBUS = True
except Exception as _e:
    print("[MAIN] modbus_master import failed:", _e)
    ModbusMaster = None
    HAS_MODBUS = False

# MicroPython 的 _thread 模块（HTTP 控制 API 用；缺时自动跳过）
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
WIFI_TIMEOUT_S = 30          # 上电连 WiFi 超时时间
LONG_PRESS_MS = 5000         # 长按 SW1 进入配网模式时间
SW_LONG_PRESS_MS = 3000      # 长按 SW 时间（全关所有继电器）
RETRY_S = 5                  # WiFi/MQTT 断线重连周期
MQTT_KEEPALIVE = 60          # MQTT 保活
MQTT_PING_S = 30             # 主动 PING 周期（须小于 keepalive）
REPORT_INTERVAL_S = 5        # 属性上报周期
CHANNEL_COUNT = 4

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

# -------------------- 全局对象 --------------------
relays = []
button = None              # SW1 Pin 对象（IO10，长按 5 秒进配网）
sw_button = None           # SW Pin 对象（IO9，短按循环、长按全关）
led = None
wlan_sta = None
wlan_ap = None
_sta_was_active = False  # 本进程是否曾对 STA 调过 active(True)；portal 收尾只碰启动过的接口
client = None
topics = None
mb_master = None
_http_srv = None             # HTTP API 监听 socket 全局引用
_http_stop = False           # HTTP API 线程退出标志（portal 切换前置 True 等线程退出）
last_ping = 0
last_report = 0
mqtt_retry = 0
wifi_retry = 0
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
def mqtt_connect(cfg):
    global client, topics, last_ping, last_report, mqtt_retry
    from umqtt.simple import MQTTClient
    topics = TopicManager(cfg["product_id"], cfg["device_id"], cfg.get("topic_mode", "direct"))
    print("MQTT topics base:", topics.base)
    try:
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
                        retain=True, qos=1)
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
        print("MQTT connected:", cfg["mqtt_host"])
        c.publish(topics.online().encode("utf-8"),
                  json.dumps({"deviceId": cfg["device_id"]}).encode("utf-8"),
                  retain=True, qos=1)
        publish_property(cfg)
        return True
    except Exception as e:
        print("MQTT connect failed:", e)
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


def publish_property(cfg, force=False):
    if client is None:
        return
    try:
        payload = build_property_payload(cfg)
        client.publish(topics.property_post().encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=1)
        print("property posted")
    except Exception as e:
        print("property post error:", e)
        raise


def publish_event(cfg, channel, state):
    if client is None:
        return
    try:
        payload = build_event_payload(cfg, channel, state)
        client.publish(topics.event("switch_change").encode("utf-8"),
                       json.dumps(payload).encode("utf-8"), qos=1)
        print("event switch_change ch%d=%s" % (channel, state))
    except Exception as e:
        print("event post error:", e)
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
                       json.dumps(payload).encode("utf-8"), qos=1)
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
                       json.dumps(payload).encode("utf-8"), qos=1)
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
                       json.dumps(payload).encode("utf-8"), qos=1)
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
    """HTTP 控制 API 路由（STA 模式下独立线程跑）

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
        }
        try:
            if wlan_sta and wlan_sta.isconnected():
                info["wifi_connected"] = True
                info["ip"] = wlan_sta.ifconfig()[0]
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
                try:
                    publish_property(cfg)
                except Exception:
                    pass
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
            try:
                publish_property(cfg)
            except Exception:
                pass
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


def start_control_http(cfg):
    """在 STA 模式下启独立 HTTP 控制 API（端口 80），用于局域网直连控制继电器

    用于 MQTT broker 下行不通时绕过 broker。线程驱动，不阻塞主循环。
    """
    if _thread is None:
        print("[HTTP API] _thread not available, skip")
        return

    # 增大线程栈，避免复杂 handler 在 _thread 里爆栈导致 socket 被默默回收
    try:
        _thread.stack_size(8 * 1024)
    except Exception:
        pass

    # 全局引用监听 socket，防止 GC 在线程异常退出时回收它
    global _http_srv, _http_stop
    _http_srv = None
    _http_stop = False  # 若上次 portal 切换置过 True，重启 HTTP 前必须复位

    def _serve():
        global _http_srv
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", 80))
            srv.listen(3)
            _http_srv = srv
            print("[HTTP API] listening on port 80")
        except Exception as e:
            print("[HTTP API] bind error:", e)
            return
        # accept 每秒超时一次，轮询 _http_stop，保证 portal 切换前能干净退出
        srv.settimeout(1)
        while not _http_stop:
            try:
                conn, addr = srv.accept()
            except OSError:
                continue
            except Exception:
                continue
            try:
                http_api_handler(cfg, conn)
            except Exception as e:
                print("[HTTP API] handler error:", e)
                try:
                    http_send(conn, "500 Internal Server Error", "err: %s" % e)
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # 被通知退出（portal 切换前）：关闭监听 socket 释放端口 80
        try:
            srv.close()
        except Exception:
            pass
        _http_srv = None
        print("[HTTP API] server stopped")

    try:
        _thread.start_new_thread(_serve, ())
        print("[HTTP API] thread started")
    except Exception as e:
        print("[HTTP API] thread fail:", e)


def portal(cfg):
    """进入 AP 配网模式

    必须在无其他线程持有网络资源时切换：STA HTTP 线程与主线程并发操作
    lwIP 会触发 Guru Meditation (Load access fault) 崩溃重启，导致热点
    一闪而逝。顺序：停 HTTP 线程 -> 断 MQTT -> 全关继电器 -> 停 Modbus
    -> 关 STA -> 开 AP。
    """
    global client, _http_stop
    # 1) 通知 HTTP 控制线程退出（accept 1s 超时轮询 _http_stop），释放端口 80。
    #    注意：经 flag 软复位的干净路径此时 _http_srv 必为 None，直接跳过等待。
    _http_stop = True
    if _http_srv is not None:
        try:
            _http_srv.close()
        except Exception:
            pass
        time.sleep(1.5)
    # 2) 断开 MQTT（幂等）
    mqtt_disconnect()
    # 3) 上电安全：继电器全部断开
    set_all_relay(False)
    # 4) 停止 Modbus 采集线程
    stop_modbus()
    # 5) 断开并关闭 STA。只在本进程曾 active(True) 过 STA 时才触碰 esp_wifi：
    #    干净 flag-boot / 无配置路径从未启动过 STA，直接 disconnect()/active(False)
    #    会让 MicroPython 在未初始化的 esp_wifi 上执行原生调用，触发
    #    Load access fault (MEPC 0x420f8b0e) Guru Meditation 硬崩溃
    #    （try/except 拦不住硬件 fault，此前热点"一闪而逝"即因此）。
    if _sta_was_active:
        try:
            wlan_sta.disconnect()
        except Exception:
            pass
        time.sleep(0.3)
        try:
            wlan_sta.active(False)
        except Exception:
            pass
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

    关键经验（v5.1 实机踩坑）：
    1. connect() 后驱动进入 connecting(1001)，此期间再调 connect() 会报
       "sta is connecting, return error / Wifi Internal Error" —— 这是正常
       拒绝，不是故障，绝不能因此触发 active(False) 重启接口（在连接中
       deinit WLAN 会 Guru Meditation 崩溃，热点/日志循环重启）。
    2. 只在驱动空闲(IDLE=1000)时才发起 connect；connecting 时只等待。
    3. 失败态(1002 密码错/1003 无AP/1004 连接失败)时 disconnect 重置再连。
    """
    import gc
    global _sta_was_active
    wlan_sta.active(True)
    _sta_was_active = True  # 记录本进程已启动过 STA（portal 收尾据此决定是否 teardown）
    time.sleep(2)  # 等驱动就绪，避免 Internal Error
    gc.collect()  # 回收碎片后再 connect，降低握手期内存压力
    start = now_ms()
    issued = False  # 本次循环是否已发出 connect
    tries = 0
    while not wlan_sta.isconnected():
        _now2 = now_ms()
        if tries >= 3:
            print("[wifi] give up after 3 tries, enter portal")
            return False
        if time.ticks_diff(_now2, start) > WIFI_TIMEOUT_S * 1000:
            try:
                print("[wifi] TIMEOUT status=%d active=%s connected=%s" % (wlan_sta.status(), wlan_sta.active(), wlan_sta.isconnected()))
            except Exception as _e:
                print("[wifi] TIMEOUT status print err:", _e)
            print("WiFi connect timeout")
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
            # （1002/1003/1004 为兼容保留）。断开重置后重试。
            print("[wifi] connect fail status", st, "- reset and retry try#%d" % (tries + 1))
            try:
                wlan_sta.disconnect()
            except Exception:
                pass
            time.sleep(2)
            issued = False
            start = now_ms()  # 重置完整超时预算，给每次尝试完整窗口
            gc.collect()
        # 短轮询等待
        for _ in range(6):
            if wlan_sta.isconnected():
                break
            time.sleep(0.5)
    print("WiFi connected:", wlan_sta.ifconfig()[0])
    return True


def led_set(on):
    if led is not None:
        led.value(0 if on else 1)


def start_modbus(cfg):
    """连上 WiFi 后启动 Modbus 采集线程（TCP/RTU 二选一，独立线程不阻塞继电器主循环）"""
    global mb_master
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
            if not HAS_MODBUS_TCP or not ModbusTCPMaster:
                print("[MAIN] ModbusTCPMaster module not available, skip")
                return False
            mb_master = ModbusTCPMaster(mb_cfg)
        else:
            if not HAS_MODBUS or not ModbusMaster:
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


def run_normal(cfg):
    global client, last_ping, last_report, mqtt_retry, wifi_retry, long_triggered, portal_requested, short_requested, sw_short_requested, mb_master, _http_stop
    if not connect_wifi(cfg):
        return False

    # 连上 WiFi 后立即启动 Modbus 采集线程，避免阻塞继电器主循环
    start_modbus(cfg)
    # 同时启 HTTP 控制 API（端口 80），用于 broker 下行不通时的局域网直连
    start_control_http(cfg)

    interval_ms = max(1, int(cfg.get("report_interval", REPORT_INTERVAL_S))) * 1000
    last_report = 0
    last_ping = now_ms()
    mqtt_retry = 0
    wifi_retry = now_ms()

    if not mqtt_connect(cfg):
        pass  # 下面循环会继续重试

    while True:
        now = now_ms()

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
        if long_triggered or portal_requested:
            long_triggered = False
            portal_requested = False
            print("SW1 long press -> portal (arm soft-reset flag)")
            # STA 热切换进 AP 时，HTTP / Modbus worker 等多线程持有网络/串口资源，
            # 与主线程并发切换 WiFi 会触发 lwIP Load access fault 崩溃（热点一闪而逝）。
            # 规避方案：写一次性标志文件后软复位，上电在无线程的干净环境直接进 portal。
            _http_stop = True  # 通知 HTTP 线程退出（尽力而为，复位后自然消失）
            try:
                with open(PORTAL_FLAG, "w") as _f:
                    _f.write("1")
            except Exception as _e:
                print("[BUTTON] write portal flag err:", _e)
            time.sleep(0.3)
            reset()

        # WiFi 断线重连
        if not wlan_sta.isconnected():
            led_set(False)
            if time.ticks_diff(now, wifi_retry) >= 0:
                wifi_retry = time.ticks_add(now, RETRY_S * 1000)
                print("WiFi lost, retrying...")
                try:
                    wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
                except Exception:
                    pass
            time.sleep(0.2)
            continue

        # MQTT 断线重连 / 维护
        if client is None:
            led_set(False)
            if time.ticks_diff(now, mqtt_retry) >= 0:
                mqtt_retry = time.ticks_add(now, RETRY_S * 1000)
                print("MQTT retry...")
                mqtt_connect(cfg)
        else:
            try:
                client.check_msg()
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

        time.sleep(0.1)


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
