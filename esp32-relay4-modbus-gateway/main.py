# -*- coding: utf-8 -*-
"""4 路继电器 + Modbus RTU 采集网关 v2.0 (MicroPython / ESP32-C3)

功能：
- 配网模式：长按 SW1 5 秒进入 AP 热点，IP 192.168.4.1，Web 配置 MQTT/产品/设备/Modbus 参数
- 正常模式：连 WiFi → 连 MQTT → 按 JetLinks 协议上报属性/事件/响应命令
- Modbus RTU 主站：独立线程轮询多从站、多寄存器，不阻塞继电器控制
- 持久化：配置写入 flash /config.json，掉电不丢失
- 断线重连：WiFi/MQTT 均支持自动重连

硬件接线（CORE-ESP32-C3 四路继电器板）：
- 继电器低电平吸合：RELAY1=IO3, RELAY2=IO4, RELAY3=IO5, RELAY4=IO7
- SW1 按键：IO10，上拉输入，按下为低电平（长按 5 秒进配网）
- LED 指示灯：IO2
- Modbus RTU (UART1)：默认 TX=IO20, RX=IO21, RS485 方向控制=IO8
"""
import json
import network
import socket
import time
import machine
from machine import Pin, reset

# 如果 modbus_master.py 已经上传到板子，则导入；否则给出占位对象，避免 main.py 直接崩溃
try:
    from modbus_master import ModbusMaster
    HAS_MODBUS = True
except Exception as _e:
    print("[MAIN] modbus_master import failed:", _e)
    ModbusMaster = None
    HAS_MODBUS = False

# -------------------- 硬件配置（按实际板子修改） --------------------
RELAY_PINS = [3, 4, 5, 7]                   # 4 路继电器 GPIO（低电平吸合）
SW1_PIN = 10                                 # 配网按键 SW1 = IO10（上拉，按下低电平）
LED_PIN = 2                                  # 状态指示灯 GPIO（IO2）
AP_SSID = "Relay4-Setuplfx"                # 配网热点名称（加了 lfx 后缀避免和别人板子冲突）
AP_IP = "192.168.4.1"

# -------------------- 运行参数 --------------------
WIFI_TIMEOUT_S = 30          # 上电连 WiFi 超时时间
LONG_PRESS_MS = 5000         # 长按 SW1 进入配网模式时间
RETRY_S = 5                  # WiFi/MQTT 断线重连周期
MQTT_KEEPALIVE = 60          # MQTT 保活
MQTT_PING_S = 30             # 主动 PING 周期（须小于 keepalive）
REPORT_INTERVAL_S = 5        # 属性上报周期
CHANNEL_COUNT = 4

CONFIG_PATH = "config.json"

# 默认 Modbus 采集配置示例：从站 1 的 0x0000 -> temperature, 0x0001 -> humidity
DEFAULT_MODBUS_CONFIG = {
    "enabled": True,
    "uart_id": 1,
    "baudrate": 9600,
    "tx_pin": 20,
    "rx_pin": 21,
    "dir_pin": 8,
    "timeout_ms": 500,
    "retries": 2,
    "slaves": [
        {
            "slave_id": 1,
            "registers": [
                {"addr": 0, "func": 3, "key": "temperature", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2},
                {"addr": 1, "func": 3, "key": "humidity", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2},
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
    "modbus": DEFAULT_MODBUS_CONFIG,
}

# -------------------- 全局对象 --------------------
relays = []
button = None
led = None
wlan_sta = None
wlan_ap = None
client = None
topics = None
mb_master = None
last_ping = 0
last_report = 0
mqtt_retry = 0
wifi_retry = 0
button_state = 1
press_start = 0
long_triggered = False

# -------------------- 工具函数 --------------------
def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = dict(DEFAULT_CONFIG)
    out.update({k: cfg[k] for k in DEFAULT_CONFIG if k in cfg})
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
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>4路继电器配置</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:20px auto">
<h2>4路继电器设备配置</h2>
<p>MAC: <b>{mac}</b><br>默认设备ID已按MAC生成，可修改。</p>
<form method="POST" action="/save">
WiFi 名称(2.4GHz):<br><input name="wifi_ssid" value="{wifi_ssid}" style="width:100%"><br><br>
WiFi 密码:<br><input name="wifi_password" type="password" value="{wifi_password}" style="width:100%"><br><br>
MQTT 服务器地址:<br><input name="mqtt_host" value="{mqtt_host}" style="width:100%"><br><br>
MQTT 端口:<br><input name="mqtt_port" value="{mqtt_port}" style="width:100%"><br><br>
MQTT 账号:<br><input name="mqtt_user" value="{mqtt_user}" style="width:100%"><br><br>
MQTT 密码:<br><input name="mqtt_password" type="password" value="{mqtt_password}" style="width:100%"><br><br>
产品ID:<br><input name="product_id" value="{product_id}" style="width:100%"><br><br>
设备ID:<br><input name="device_id" value="{device_id}" style="width:100%"><br><br>
主题模式:<br>
<select name="topic_mode" style="width:100%">
<option value="direct" {sel_direct}>direct（/product/device/property/post，对接现有 EMQX 规则）</option>
<option value="sys" {sel_sys}>sys（/sys/...，JetLinks MQTT 网关规范）</option>
</select><br><br>
上报周期(秒):<br><input name="report_interval" value="{report_interval}" style="width:100%"><br><br>
<hr>
<h3>Modbus RTU 采集网关配置</h3>
<p style="color:#666;font-size:12px">下方填写 JSON，示例：采集从站1的 0x0000(temperature)、0x0001(humidity)，周期 1000ms</p>
<textarea name="modbus_json" rows="14" style="width:100%;font-family:monospace;font-size:12px">{modbus_json}</textarea><br><br>
<button style="padding:10px 24px;font-size:16px">保存并重启</button>
</form>
</body></html>"""


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
    # 只填充 PAGE 模板需要的字段，避免 relay_pins/sw1_pin 这些硬件常量
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
        d["modbus_json"] = json.dumps(cfg.get("modbus", DEFAULT_MODBUS_CONFIG))
    except Exception:
        d["modbus_json"] = json.dumps(DEFAULT_MODBUS_CONFIG)
    d["mac"] = mac_str()
    d["sel_direct"] = 'selected' if cfg.get("topic_mode", "direct") == "direct" else ""
    d["sel_sys"] = 'selected' if cfg.get("topic_mode", "direct") == "sys" else ""
    return PAGE.format(**d)


def portal(cfg):
    """进入 AP 配网模式"""
    global client
    mqtt_disconnect()
    set_all_relay(False)
    wlan_sta.active(False)
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password="", authmode=network.AUTH_OPEN)
    try:
        ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
    except Exception:
        pass
    print("配网模式: 连接热点 %s，打开 http://%s" % (AP_SSID, AP_IP))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", 80))
    except Exception as e:
        print("bind error:", e)
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
        else:
            http_send(conn, "404 Not Found", "404")


# -------------------- WiFi / 正常模式 --------------------
def connect_wifi(cfg):
    wlan_sta.active(True)
    time.sleep(2)  # 避免 Wifi Internal Error
    start = now_ms()
    while not wlan_sta.isconnected():
        if time.ticks_diff(now_ms(), start) > WIFI_TIMEOUT_S * 1000:
            print("WiFi connect timeout")
            return False
        try:
            wlan_sta.connect(cfg["wifi_ssid"], cfg["wifi_password"])
        except Exception as e:
            print("wifi connect exception:", e)
        for _ in range(10):
            if wlan_sta.isconnected():
                break
            time.sleep(0.5)
    print("WiFi connected:", wlan_sta.ifconfig()[0])
    return True


def led_set(on):
    if led is not None:
        led.value(0 if on else 1)


def start_modbus(cfg):
    """连上 WiFi 后启动 Modbus 采集线程（UART 不依赖 WiFi，但在这里启动便于现场日志查看）"""
    global mb_master
    if not HAS_MODBUS or not ModbusMaster:
        print("[MAIN] Modbus module not available, skip")
        return False
    mb_cfg = cfg.get("modbus") or DEFAULT_MODBUS_CONFIG
    if not mb_cfg.get("enabled", True):
        print("[MAIN] Modbus disabled in config")
        return False
    # 如果已经启动过则先停止，再重新初始化（配置可能已变）
    if mb_master is not None:
        try:
            mb_master.stop()
        except Exception as e:
            print("[MAIN] stop old modbus master error:", e)
        mb_master = None
    try:
        mb_master = ModbusMaster(mb_cfg)
        ok = mb_master.init_hw() and mb_master.start()
        print("[MAIN] start_modbus result:", ok)
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
    global client, last_ping, last_report, mqtt_retry, wifi_retry, long_triggered, mb_master
    if not connect_wifi(cfg):
        return False

    # 连上 WiFi 后立即启动 Modbus 采集线程，避免阻塞继电器主循环
    start_modbus(cfg)

    interval_ms = max(1, int(cfg.get("report_interval", REPORT_INTERVAL_S))) * 1000
    last_report = 0
    last_ping = now_ms()
    mqtt_retry = 0
    wifi_retry = now_ms()

    if not mqtt_connect(cfg):
        pass  # 下面循环会继续重试

    while True:
        now = now_ms()

        # 检查 SW1 长按进配网
        handle_button(cfg)
        if long_triggered:
            long_triggered = False
            print("SW1 long press -> portal")
            mqtt_disconnect()
            stop_modbus()
            try:
                wlan_sta.disconnect()
            except Exception:
                pass
            return False

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
def handle_button(cfg):
    global button_state, press_start, long_triggered
    st = button.value()
    now = now_ms()
    if st == 0 and button_state == 1:
        press_start = now
        long_triggered = False
    if st == 0 and not long_triggered:
        if time.ticks_diff(now, press_start) >= LONG_PRESS_MS:
            long_triggered = True
    if st == 1 and button_state == 0:
        pass  # 松开，不足 5 秒无动作
    button_state = st


# -------------------- 主入口 --------------------
def main():
    global wlan_sta, wlan_ap, button, led
    cfg = load_config()
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_ap = network.WLAN(network.AP_IF)

    # 初始化硬件
    init_relays(cfg.get("relay_pins"))
    sw1 = int(cfg.get("sw1_pin", SW1_PIN))
    button = Pin(sw1, Pin.IN, Pin.PULL_UP)
    if LED_PIN is not None:
        led = Pin(LED_PIN, Pin.OUT, value=1)  # 熄灭（假设低电平亮）

    # 上电安全：继电器全部断开
    set_all_relay(False)

    # 无配置 / 连不上 WiFi -> 进入配网
    if not cfg.get("wifi_ssid"):
        print("no wifi config, enter portal")
        stop_modbus()
        portal(cfg)
        return

    while True:
        ok = run_normal(cfg)
        if not ok:
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
