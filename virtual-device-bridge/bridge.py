#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
虚拟设备映射桥接器 (Virtual Device Bridge)

功能：
1. 订阅 4 路继电器/Modbus 网关（父设备）的上行属性和事件。
2. 按配置把父设备的一包 properties 拆分为多个子设备属性/事件，
   分别发布到对应产品/设备的 MQTT topic。
3. 订阅各虚拟子设备的下行命令 topic，映射回父设备的 set_channel/switch_all 命令。
4. 模拟人体感应器、烟雾感应器等额外传感器定时上报。
5. 提供 HTTP API 查看桥接器运行状态与统计。

与 JetLinks 配合：
- 父设备对应“继电器网关产品”下的一台真实设备。
- 每个虚拟设备需要在 JetLinks 中预先创建对应产品/设备，并配置物模型属性。
- 桥接器本身不保存状态，所有数据来自 MQTT，可水平扩展（不同 client_id）。
"""
import json
import os
import random
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional

import yaml

try:
    import paho.mqtt.client as mqtt
except ImportError as e:  # pragma: no cover
    sys.exit("缺少 paho-mqtt，请先执行: pip install paho-mqtt>=1.5 pyyaml")

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(level: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    print(f"[{ts}][{level}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("mqtt", {})
    cfg["mqtt"].setdefault("broker", "127.0.0.1")
    cfg["mqtt"].setdefault("port", 1883)
    cfg["mqtt"].setdefault("username", "")
    cfg["mqtt"].setdefault("password", "")
    cfg["mqtt"].setdefault("client_id", "virtual-bridge-01")
    cfg["mqtt"].setdefault("qos", 1)

    cfg.setdefault("gateway", {})
    # 默认只关联本组（admin5 / -lfx）在 JetLinks 上创建的产品与设备，避免误连其他组资源
    cfg["gateway"].setdefault("product_id", "relay4_lfx")
    cfg["gateway"].setdefault("device_id", "7ce8b1c1a7fc")

    cfg.setdefault("child_topic_mode", "direct")
    cfg.setdefault("relays", [])
    cfg.setdefault("sensors", [])
    cfg.setdefault("simulated", [])
    cfg.setdefault("http_api", {"enabled": True, "host": "0.0.0.0", "port": 8080})

    # v6.0.4 网关链路自愈：定时向父设备下行 __hb__ 心跳，让板子的 MQTT
    # 收发路径保持活跃；一旦父设备上行停滞，自动切到加速心跳把链路"拍醒"。
    hb = cfg.setdefault("gateway_heartbeat", {})
    hb.setdefault("enabled", True)
    hb.setdefault("interval_seconds", 45)         # 常规心跳周期
    hb.setdefault("stall_after_seconds", 40)      # 超过该时长没收到父设备属性 -> 判停滞
    hb.setdefault("stall_interval_seconds", 10)   # 停滞时的加速心跳周期
    hb.setdefault("function_id", "__hb__")
    return cfg


# ---------------------------------------------------------------------------
# Topic 构造
# ---------------------------------------------------------------------------
class TopicHelper:
    def __init__(self, mode: str):
        self.mode = mode

    def base(self, product_id: str, device_id: str) -> str:
        if self.mode == "sys":
            return f"/sys/{product_id}/{device_id}"
        return f"/{product_id}/{device_id}"

    def property_post(self, product_id: str, device_id: str) -> str:
        """Where the bridge PUBLISHES child property reports to.
        - direct:  /{pid}/{did}/property/post
        - sys:     /sys/{pid}/{did}/thing/event/property/post
        - jetlinks:/{pid}/{did}/properties/report
        """
        base = self.base(product_id, device_id)
        if self.mode == "sys":
            return f"{base}/thing/event/property/post"
        if self.mode == "jetlinks":
            return f"{base}/properties/report"
        return f"{base}/property/post"

    def event(self, product_id: str, device_id: str, event_id: str) -> str:
        base = self.base(product_id, device_id)
        if self.mode == "sys":
            return f"{base}/thing/event/{event_id}"
        if self.mode == "jetlinks":
            # JetLinks does not define a dedicated per-event topic; use the
            # standard property-report as a fallback (events can also ride on
            # property/report with an 'events' list).
            return f"{base}/properties/report"
        return f"{base}/event/{event_id}"

    def service_cmd(self, product_id: str, device_id: str) -> str:
        """Where the bridge SUBSCRIBES for child DOWNLINK commands.
        - direct:  /{pid}/{did}/service/cmd
        - sys:     /sys/{pid}/{did}/thing/service/property/set
        - jetlinks:/{pid}/{did}/function/invoke
        """
        base = self.base(product_id, device_id)
        if self.mode == "sys":
            return f"{base}/thing/service/property/set"
        if self.mode == "jetlinks":
            return f"{base}/function/invoke"
        return f"{base}/service/cmd"

    def function_invoke(self, product_id: str, device_id: str, function_id: str) -> str:
        base = self.base(product_id, device_id)
        return f"{base}/thing/service/{function_id}/invoke"


class GatewayTopicHelper:
    """Separate topic helper for the parent gateway: 4-real-relay firmware
    uses Aliyun-style /property/post and /service/cmd regardless of the
    JetLinks sub-device mode."""
    def __init__(self, mode: str = "direct"):
        self.mode = mode
        self._helper = TopicHelper(mode)

    def property_post(self, product_id: str, device_id: str) -> str:
        return self._helper.property_post(product_id, device_id)

    def service_cmd(self, product_id: str, device_id: str) -> str:
        return self._helper.service_cmd(product_id, device_id)


# ---------------------------------------------------------------------------
# 模拟数据生成器
# ---------------------------------------------------------------------------
def generate_simulated_value(spec: Dict[str, Any]) -> Any:
    t = spec.get("type", "random_int")
    if t == "random_int":
        return random.randint(int(spec.get("min", 0)), int(spec.get("max", 100)))
    if t == "random_float":
        return round(random.uniform(float(spec.get("min", 0)), float(spec.get("max", 100))), 2)
    if t == "random_choice":
        choices = spec.get("choices", [0, 1])
        weights = spec.get("weights")
        return random.choices(choices, weights=weights, k=1)[0]
    if t == "const":
        return spec.get("value")
    return None


# ---------------------------------------------------------------------------
# 桥接器核心
# ---------------------------------------------------------------------------
class VirtualDeviceBridge:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.mqtt_cfg = cfg["mqtt"]
        self.gateway = cfg["gateway"]
        self.topic_mode = cfg.get("child_topic_mode", "direct")
        self.topic = TopicHelper(self.topic_mode)
        # Parent gateway uses its own mode (typically "direct" — Aliyun-style,
        # since 4-real-relay firmware posts to /property/post).
        self.gw_topic = GatewayTopicHelper(
            cfg.get("gateway_topic_mode", "direct"))
        self.qos = int(self.mqtt_cfg.get("qos", 1))

        self.client: Optional[mqtt.Client] = None
        self.lock = threading.Lock()
        self.connected = False

        # 统计
        self.stats = {
            "up_property": 0,   # 收到父设备属性包数
            "up_event": 0,      # 收到父设备事件数
            "down_property": 0, # 转发给子设备属性包数
            "down_event": 0,    # 转发给子设备事件数
            "down_cmd": 0,      # 收到子设备命令数
            "up_cmd": 0,        # 转发给父设备命令数
            "sim_post": 0,      # 模拟传感器上报数
            "errors": 0,
            "hb_sent": 0,       # v6.0.4 下发网关心跳次数
            "hb_ack": 0,        # v6.0.4 收到板子心跳应答次数
            "stall_events": 0,  # v6.0.4 父设备上行停滞次数
        }

        # v6.0.4 网关链路存活追踪（毫秒时间戳）
        self.last_gateway_property_ms = time.time() * 1000
        self.last_hb_ack_ms = 0
        self._last_hb_sent_ms = 0
        self._last_stall_log_ms = 0

        # 快速索引：父属性 key -> [(relay_index, child_key)]
        self.relay_prop_index: Dict[str, List[tuple]] = defaultdict(list)
        # 通道 -> relay 配置索引
        self.channel_to_relay: Dict[int, Dict[str, Any]] = {}
        # 子设备标识 -> relay 配置
        self.relay_by_child: Dict[str, Dict[str, Any]] = {}
        # 传感器索引
        self.sensor_patterns: List[Dict[str, Any]] = []
        self.sensor_exact: Dict[str, Dict[str, Any]] = {}

        self._build_indexes()
        self._start_http_api()
        self._start_simulated_timers()
        self._start_heartbeat()

    # ---------------- 索引构建 ----------------
    def _build_indexes(self):
        for r in self.cfg.get("relays", []):
            ch = int(r["channel"])
            self.channel_to_relay[ch] = r
            child_key = f"{r['product_id']}/{r['device_id']}"
            self.relay_by_child[child_key] = r
            for src, dst in r.get("property_map", {}).items():
                self.relay_prop_index[src].append((r, dst))

        for s in self.cfg.get("sensors", []):
            pattern = s.get("source_pattern", "")
            if "*" in pattern or "?" in pattern:
                self.sensor_patterns.append(s)
            else:
                # 未使用通配时按精确 key 处理（兼容旧配置）
                self.sensor_exact[pattern] = s

    # ---------------- MQTT 生命周期 ----------------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            log("INFO", f"MQTT connected (flags={flags})")
            self._subscribe_all()
        else:
            self.connected = False
            log("ERROR", f"MQTT connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        self.connected = False
        log("WARN", f"MQTT disconnected, rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8", errors="replace")
            data = json.loads(payload) if payload else {}
        except Exception as e:
            log("ERROR", f"消息解析失败 topic={msg.topic}: {e}")
            self.stats["errors"] += 1
            return

        # 父设备上行属性
        if topic == self.gw_topic.property_post(self.gateway["product_id"], self.gateway["device_id"]):
            self._handle_gateway_property(data)
            return

        # 父设备上行事件
        gw_base = self.gw_topic._helper.base(self.gateway["product_id"], self.gateway["device_id"])
        if topic.startswith(gw_base + "/") and "/event/" in topic:
            self._handle_gateway_event(topic, data)
            return

        # 子设备下行命令
        for child_key, r in self.relay_by_child.items():
            product_id, device_id = child_key.split("/", 1)
            cmd_topic = self.topic.service_cmd(product_id, device_id)
            if topic == cmd_topic:
                self._handle_child_command(product_id, device_id, data)
                return

        log("DEBUG", f"未处理消息: {topic}")

    def _subscribe_all(self):
        if not self.client:
            return
        # 父设备上行
        r1 = self.client.subscribe(
            self.gw_topic.property_post(self.gateway["product_id"], self.gateway["device_id"]),
            qos=self.qos,
        )
        log("INFO", f"sub parent {self.gw_topic.property_post(self.gateway['product_id'], self.gateway['device_id'])} -> result={r1}")
        # 父设备事件通配
        gw_base = self.gw_topic._helper.base(self.gateway["product_id"], self.gateway["device_id"])
        if self.gw_topic.mode == "sys":
            event_wild = f"{gw_base}/thing/event/+"
        elif self.gw_topic.mode == "jetlinks":
            # JetLinks uses the same topic for events (within properties/report
            # payload) plus an optional message/event sub-topic; subscribe to
            # both to be safe.
            event_wild = f"{gw_base}/properties/report"
        else:
            event_wild = f"{gw_base}/event/+"
        self.client.subscribe(event_wild, qos=self.qos)

        # 各子设备下行命令
        for r in self.cfg.get("relays", []):
            if not r.get("command_map"):
                continue
            cmd_topic = self.topic.service_cmd(r["product_id"], r["device_id"])
            rs = self.client.subscribe(cmd_topic, qos=self.qos)
            log("INFO", f"subscribe child cmd: {cmd_topic} -> result={rs}")

        log("INFO", f"subscribed parent property & events, child commands")

    def connect(self):
        # 2026-09-10 端到端验证通过（v6.0.3），要点记录：
        # 1) JetLinks 平台真实下行 topic 是「带前导斜杠」的
        #    /{productId}/{deviceId}/function/invoke，与本文件 service_cmd()
        #    构造一致。此前"收不到下行"的误诊源于诊断脚本发的是不带斜杠的
        #    topic（MQTT 里是两个不同 topic）。
        # 2) client_id 拼上 PID：调试期间曾出现同机多个 bridge 进程共用同一
        #    client_id 被 broker 互踢的情况，加 PID 后天然不冲突。
        # 3) 使用 paho v1 默认 API（回调签名 v1 风格）；paho>=2 装了也能跑，
        #    只是打 DeprecationWarning。
        base_client_id = self.mqtt_cfg["client_id"]
        final_client_id = f"{base_client_id}-{os.getpid()}"
        log("INFO", f"this process PID={os.getpid()} using client_id={final_client_id}")
        self.client = mqtt.Client(
            client_id=final_client_id,
            clean_session=True,
        )
        user = self.mqtt_cfg.get("username", "")
        pwd = self.mqtt_cfg.get("password", "")
        if user:
            self.client.username_pw_set(user, pwd)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.client.connect(
            self.mqtt_cfg["broker"],
            int(self.mqtt_cfg["port"]),
            keepalive=60,
        )
        self.client.loop_start()

    # ---------------- 上行：父设备属性拆分 ----------------
    def _handle_gateway_property(self, data: Dict[str, Any]):
        self.stats["up_property"] += 1
        # v6.0.4：刷新父设备上行存活时间（心跳/停滞判定的依据）
        self.last_gateway_property_ms = time.time() * 1000
        parent_props = data.get("properties", {})
        if not isinstance(parent_props, dict):
            return

        # 方案A：强制用本机(PC)时间，避免固件 RTC 时间错误(如 2031 年)污染 JetLinks 属性时间
        ts = int(time.time() * 1000)

        # 1) 继电器通道属性：按 property_map 拆到 4 个子设备
        child_props: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for src_key, mappings in self.relay_prop_index.items():
            if src_key not in parent_props:
                continue
            for relay_cfg, child_attr in mappings:
                child_key = f"{relay_cfg['product_id']}/{relay_cfg['device_id']}"
                child_props[child_key][child_attr] = parent_props[src_key]

        # 2) Modbus 传感器：按 source_pattern / source_key 匹配
        sensor_props: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for src_key, val in parent_props.items():
            # 精确匹配
            s = self.sensor_exact.get(src_key)
            if s:
                child_key = f"{s['product_id']}/{s['device_id']}"
                mapped = s.get("property_map", {}).get(src_key)
                if mapped:
                    sensor_props[child_key][mapped] = val
                continue

            # 通配匹配
            for s in self.sensor_patterns:
                pattern = s.get("source_pattern", "")
                if fnmatch(src_key, pattern):
                    child_key = f"{s['product_id']}/{s['device_id']}"
                    mapping = s.get("property_map", {})
                    # 优先精确映射；没有时尝试去掉 sN_ 前缀作为属性名
                    if src_key in mapping:
                        sensor_props[child_key][mapping[src_key]] = val
                    else:
                        # 例如 s1_temperature -> temperature
                        if "_" in src_key:
                            raw = src_key.split("_", 1)[1]
                            if raw:
                                sensor_props[child_key][raw] = val
                                sensor_props[child_key][src_key] = val
                    break

        # 合并并发布
        all_child_props: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for k, v in child_props.items():
            all_child_props[k].update(v)
        for k, v in sensor_props.items():
            all_child_props[k].update(v)

        for child_key, props in all_child_props.items():
            if not props:
                continue
            product_id, device_id = child_key.split("/", 1)
            payload = {
                "productId": product_id,
                "deviceId": device_id,
                "timestamp": ts,
                "properties": props,
            }
            self._publish(self.topic.property_post(product_id, device_id), payload)
            self.stats["down_property"] += 1

    # ---------------- 上行：父设备事件转发 ----------------
    def _handle_gateway_event(self, topic: str, data: Dict[str, Any]):
        self.stats["up_event"] += 1
        # 提取 event_id
        gw_base = self.gw_topic._helper.base(self.gateway["product_id"], self.gateway["device_id"])
        prefix = f"{gw_base}/{'thing/event/' if self.gw_topic.mode == 'sys' else 'event/'}"
        event_id = topic[len(prefix):] if topic.startswith(prefix) else ""
        if not event_id:
            return

        # v6.0.4：板子对网关心跳的应答——只用来判定"板子收+发双向都在线"，
        # 不转发给任何子设备。
        if event_id == "hb_ack":
            self.stats["hb_ack"] += 1
            self.last_hb_ack_ms = time.time() * 1000
            log("DEBUG", f"gateway hb_ack #{self.stats['hb_ack']} data={data.get('data')}")
            return

        event_data = data.get("data", {})
        ch = event_data.get("channel")
        state = event_data.get("state")
        if ch is None:
            return

        r = self.channel_to_relay.get(int(ch))
        if not r:
            return

        event_map = r.get("event_map", {}).get(event_id)
        if not event_map:
            # 未配置事件映射时，直接把原事件转发到子设备（方便调试）
            child_event_id = event_id
            child_data = event_data
        else:
            child_event_id = event_map.get("event_id", event_id)
            child_data = {}
            for src_k, dst_k in event_map.get("data_map", {}).items():
                child_data[dst_k] = event_data.get(src_k)

        payload = {
            "productId": r["product_id"],
            "deviceId": r["device_id"],
            "timestamp": int(time.time() * 1000),  # 方案A：沿用本机时间，忽略固件时间戳
            "eventId": child_event_id,
            "data": child_data,
        }
        self._publish(self.topic.event(r["product_id"], r["device_id"], child_event_id), payload)
        self.stats["down_event"] += 1

    # ---------------- 下行：子设备命令聚合 ----------------
    def _handle_child_command(self, product_id: str, device_id: str, data: Dict[str, Any]):
        self.stats["down_cmd"] += 1
        child_key = f"{product_id}/{device_id}"
        r = self.relay_by_child.get(child_key)
        if not r:
            log("WARN", f"未找到子设备命令映射: {child_key}")
            return

        # 取 functionId / method；兼容属性写（properties 在 payload 里）
        method = data.get("functionId") or data.get("method") or ""
        message_id = data.get("messageId") or data.get("id") or ""
        params: Dict[str, Any] = {}

        inputs = data.get("inputs")
        if isinstance(inputs, list):
            for it in inputs:
                if isinstance(it, dict) and "name" in it:
                    params[it["name"]] = it.get("value")
        if isinstance(data.get("params"), dict):
            params.update(data["params"])
        # 属性写：{properties: {lockState: true}}
        if isinstance(data.get("properties"), dict):
            for k, v in data["properties"].items():
                if not method:
                    method = k  # 用属性名作为 method，去 command_map 匹配
                params.setdefault("value", v)

        cmd_map = r.get("command_map", {})
        mapping = cmd_map.get(method)
        if not mapping:
            log("WARN", f"子设备 {child_key} 未定义命令映射: {method}")
            return

        function_id = mapping.get("function_id", "set_channel")
        tpl_inputs = mapping.get("inputs", {})
        gateway_inputs = []
        for name, val in tpl_inputs.items():
            # 支持模板占位符 {value}
            if isinstance(val, str) and val == "{value}":
                val = params.get("value")
            gateway_inputs.append({"name": name, "value": val})

        gateway_cmd = {
            "messageId": message_id,
            "functionId": function_id,
            "inputs": gateway_inputs,
        }
        self._publish(
            self.gw_topic.service_cmd(self.gateway["product_id"], self.gateway["device_id"]),
            gateway_cmd,
        )
        self.stats["up_cmd"] += 1
        log("INFO", f"聚合命令 {child_key}/{method} -> gateway/{function_id} ch={r['channel']}")

    # ---------------- 模拟传感器 ----------------
    def _start_simulated_timers(self):
        for sim in self.cfg.get("simulated", []):
            t = threading.Thread(
                target=self._sim_loop,
                args=(sim,),
                daemon=True,
                name=f"sim-{sim.get('device_id')}",
            )
            t.start()
            log("INFO", f"启动模拟传感器 {sim['device_id']} 周期={sim.get('interval_seconds', 5)}s")

    def _sim_loop(self, sim: Dict[str, Any]):
        interval = float(sim.get("interval_seconds", 5))
        product_id = sim["product_id"]
        device_id = sim["device_id"]
        while True:
            time.sleep(interval)
            if not self.connected:
                continue
            props = {}
            for p in sim.get("properties", []):
                props[p["key"]] = generate_simulated_value(p)
            if not props:
                continue
            payload = {
                "productId": product_id,
                "deviceId": device_id,
                "timestamp": int(time.time() * 1000),
                "properties": props,
            }
            self._publish(self.topic.property_post(product_id, device_id), payload)
            self.stats["sim_post"] += 1
            log("DEBUG", f"模拟上报 {device_id}: {props}")

    # ---------------- v6.0.4 网关心跳（MQTT 链路自愈） ----------------
    def _start_heartbeat(self):
        hb = self.cfg.get("gateway_heartbeat", {}) or {}
        if not hb.get("enabled", True):
            log("INFO", "网关心跳已禁用 (gateway_heartbeat.enabled=false)")
            return
        t = threading.Thread(target=self._heartbeat_loop, daemon=True,
                             name="gw-heartbeat")
        t.start()
        log("INFO", "网关心跳已启动: 常规 %ss / 停滞 %ss（超 %ss 未收到父设备属性即判停滞，"
                    "停滞值随心跳下发板子触发其自愈重连）"
            % (hb.get("interval_seconds", 45),
               hb.get("stall_interval_seconds", 10),
               hb.get("stall_after_seconds", 40)))

    def _send_gateway_heartbeat(self):
        """向父设备下发一帧空操作心跳，强制板子走一遍"收 -> 回"的完整 MQTT 路径。

        板子固件识别 functionId=__hb__ 后立即回 /event/hb_ack，因此 hb_ack 的
        到达说明板子的收/发两个方向都真的活着——这正是 bridge 单靠上行属性
        无法判断的事情。

        payload 额外带 stall_s = 本进程眼中「父设备属性已停滞多少秒」。
        板子无法察觉自己的上行被静默丢弃（publish 不抛异常、心跳也照常往返），
        只有接收端 bridge 知道真相，所以由这里把读数回传；板子据此超阈值主动
        断开重连。这是 2026-09-10 实板排查出的「bridge 报停滞 -> 板子自愈」闭环。
        """
        hb = self.cfg.get("gateway_heartbeat", {}) or {}
        self.stats["hb_sent"] += 1
        stall_s = int(max(0.0, (time.time() * 1000 - self.last_gateway_property_ms) / 1000.0))
        payload = {
            "messageId": "hb-%d" % self.stats["hb_sent"],
            "functionId": hb.get("function_id", "__hb__"),
            "inputs": [],
            "stall_s": min(stall_s, 86400),
        }
        self._publish(
            self.gw_topic.service_cmd(self.gateway["product_id"], self.gateway["device_id"]),
            payload,
        )
        self._last_hb_sent_ms = time.time() * 1000

    def _heartbeat_loop(self):
        hb = self.cfg.get("gateway_heartbeat", {}) or {}
        interval_ms = max(1, int(hb.get("interval_seconds", 45))) * 1000
        stall_after_ms = max(1, int(hb.get("stall_after_seconds", 40))) * 1000
        stall_interval_ms = max(1, int(hb.get("stall_interval_seconds", 10))) * 1000
        # 上电初期不打扰：等板子的首包属性上行到位再开始打心跳
        time.sleep(3)
        while True:
            try:
                if not self.connected:
                    time.sleep(1)
                    continue
                now = time.time() * 1000
                age = now - self.last_gateway_property_ms
                stalled = age >= stall_after_ms
                interval = stall_interval_ms if stalled else interval_ms
                if now - self._last_hb_sent_ms >= interval:
                    if stalled and now - self._last_stall_log_ms >= 30000:
                        self._last_stall_log_ms = now
                        self.stats["stall_events"] += 1
                        # 板子心跳还在回、属性却停滞 => 上行静默丢失。stall_s 已随
                        # 心跳回传给板子，由它自主断开重连（板子侧无法自查这类故障）。
                        ack_age = (now - self.last_hb_ack_ms) if self.last_hb_ack_ms else None
                        log("WARN", "父设备上行停滞 %.0fs（板子心跳 %s）-> 已随心跳下发 stall_s，"
                                    "等待板子自愈"
                            % (age / 1000.0,
                               ("%.0fs 前" % (ack_age / 1000.0)) if ack_age is not None
                               else "未收到过"))
                    self._send_gateway_heartbeat()
                time.sleep(1)
            except Exception as e:
                log("ERROR", f"网关心跳线程异常: {e}")
                time.sleep(3)

    # ---------------- 发布封装 ----------------
    def _publish(self, topic: str, payload: Dict[str, Any]):
        if not self.client or not self.connected:
            return
        try:
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=self.qos)
        except Exception as e:
            log("ERROR", f"publish failed {topic}: {e}")
            self.stats["errors"] += 1

    # ---------------- HTTP API ----------------
    def _start_http_api(self):
        api_cfg = self.cfg.get("http_api", {})
        if not api_cfg.get("enabled", True):
            return
        try:
            from flask import Flask, jsonify
        except ImportError:
            log("WARN", "Flask 未安装，HTTP API 已禁用。需要时执行: pip install flask")
            return

        app = Flask(__name__)

        @app.route("/health", methods=["GET"])
        def health():
            now_ms = time.time() * 1000
            hb = self.cfg.get("gateway_heartbeat", {}) or {}
            return jsonify({
                "status": "ok" if self.connected else "disconnected",
                "connected": self.connected,
                "gateway": self.gateway,
                "stats": self.stats,
                # v6.0.4 链路存活诊断
                "gateway_link": {
                    "last_property_age_s": round((now_ms - self.last_gateway_property_ms) / 1000.0, 1),
                    "last_hb_ack_age_s": (round((now_ms - self.last_hb_ack_ms) / 1000.0, 1)
                                          if self.last_hb_ack_ms else None),
                    "stalled": (now_ms - self.last_gateway_property_ms)
                               >= max(1, int(hb.get("stall_after_seconds", 40))) * 1000,
                    "heartbeat_interval_s": hb.get("interval_seconds", 45),
                },
            })

        @app.route("/config", methods=["GET"])
        def get_config():
            return jsonify(self.cfg)

        def run():
            app.run(
                host=api_cfg.get("host", "0.0.0.0"),
                port=int(api_cfg.get("port", 8080)),
                threaded=True,
                use_reloader=False,
            )

        t = threading.Thread(target=run, daemon=True, name="http-api")
        t.start()
        log("INFO", f"HTTP API 启动: http://{api_cfg.get('host','0.0.0.0')}:{api_cfg.get('port',8080)}/health")

    # ---------------- 运行 ----------------
    def run(self):
        self.connect()
        log("INFO", "Virtual Device Bridge started. Press Ctrl+C to stop.")
        last_warn_ms = 0
        try:
            while True:
                time.sleep(1)
                if not self.connected:
                    # 每 30s 最多打一条，避免断线期间刷屏把日志冲爆
                    now_ms = time.time() * 1000
                    if now_ms - last_warn_ms >= 30000:
                        last_warn_ms = now_ms
                        log("WARN", "等待 MQTT 重连...（paho 自动重连中）")
        except KeyboardInterrupt:
            log("INFO", "正在停止...")
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    log("INFO", f"bridge 启动 pid={os.getpid()} cwd={os.getcwd()} config={config_path}")
    if not os.path.exists(config_path):
        sys.exit(f"配置文件不存在: {config_path}")
    cfg = load_config(config_path)
    bridge = VirtualDeviceBridge(cfg)
    bridge.run()


if __name__ == "__main__":
    # v6.0.4：进程级兜底——异常退出时把堆栈写进日志（2026-09-10 曾出现
    # bridge 夜间静默死亡、日志无任何线索），并返回非 0 退出码，
    # 便于 _run_supervised.cmd 之类的守护脚本自动拉起。
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        log("FATAL", "bridge 异常退出，堆栈如下：")
        traceback.print_exc()
        sys.exit(1)
