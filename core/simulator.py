# -*- coding: utf-8 -*-
"""8 路继电器 MQTT 模拟器核心类（RelaySimulator）

链路设计（对齐 JetLinks demo 完整物模型；产品 relay8_lfx / 设备 RELAY8-TERM-01）：

  模拟器 --/relay8_lfx/{device}/property/post--------> EMQX rule_lfx_relay8_property
      -> /relay8_lfx/RELAY8-TERM-01/properties/report (JetLinks 入库，UI 运行状态可见)
  模拟器 --/relay8_lfx/{device}/event/switch_change--- 直接发布（网关通配订阅 /event/+）
      -> JetLinks 记录 switch_change 事件（不经 EMQX 规则，避免脏 eventId=post 与重复）
  模拟器 --/relay8_lfx/{device}/function/post---------> EMQX rule_lfx_relay8_reply
      -> /relay8_lfx/RELAY8-TERM-01/function/invoke/reply (JetLinks 收命令响应)
  JetLinks --/relay8_lfx/RELAY8-TERM-01/function/invoke--> EMQX rule_lfx_relay8_cmd
      -> /relay8_lfx/RELAY8-TERM-01/service/cmd           (模拟器收 set_channel/switch_all)
  模拟器直连订阅 JetLinks 规范化属性读/写主题（properties/read、properties/write），
  并在 properties/read/reply、properties/write/reply 上回执。

功能：8 通道状态/电压/电流/功率 + 温湿度模拟、定时上报(默认5s)、状态变化立即上报、
      变化触发 switch_change 事件、set_channel/switch_all 控制及回复、MQTT 自动重连。
"""
import json
import logging
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt

# 兼容直接运行：python core/simulator.py 时也能找到项目根目录下的 config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core.channel_bank import RelayChannelBank  # noqa: E402

log = logging.getLogger("relay8.simulator")


class RelaySimulator:
    def __init__(self, device_id=None, product_id=None, host=None, port=None,
                 username=None, password=None, interval=5.0):
        self.device_id = device_id or config.DEVICE_ID
        self.product_id = product_id or config.PRODUCT_ID
        self.interval = max(1.0, float(interval))
        self.bank = RelayChannelBank()
        self.lock = threading.Lock()

        p = f"/{self.product_id}/{self.device_id}"
        self.topic_property = p + "/property/post"      # -> EMQX 规则 -> properties/report
        self.topic_event = p + "/event/switch_change"   # 直接发布（网关按 /event/+ 识别）
        self.topic_reply = p + "/function/post"         # -> EMQX 规则 -> function/invoke/reply
        self.topic_cmd = p + "/service/cmd"             # EMQX 规则 <- function/invoke
        # JetLinks 规范化下行主题（设备直连订阅）
        self.topic_online = p + "/online"
        self.topic_offline = p + "/offline"
        self.topic_read = p + "/properties/read"
        self.topic_write = p + "/properties/write"
        self.topic_read_reply = p + "/properties/read/reply"
        self.topic_write_reply = p + "/properties/write/reply"

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.device_id, clean_session=True,
            protocol=mqtt.MQTTv311)
        self.client.username_pw_set(username or config.MQTT_USER,
                                    password or config.MQTT_PASS)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.will_set(self.topic_offline,
                             json.dumps({"deviceId": self.device_id}), qos=1)
        self._host = host or config.MQTT_HOST
        self._port = port or config.MQTT_PORT
        self._stop = threading.Event()

    # ---------- MQTT 回调 ----------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("已连接 MQTT %s:%d", self._host, self._port)
            for tp in (self.topic_cmd, self.topic_read, self.topic_write):
                client.subscribe(tp, qos=1)
            log.info("订阅下行: %s, %s, %s",
                     self.topic_cmd, self.topic_read, self.topic_write)
            # 先发上线通知 /online，再立即上报一次属性（断开重连时同样生效）
            client.publish(self.topic_online,
                           json.dumps({"deviceId": self.device_id}), qos=1)
            self.publish_property()
        else:
            log.error("连接失败 rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        if rc != 0 and not self._stop.is_set():
            log.warning("连接断开 rc=%s, 等待自动重连", rc)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            log.error("命令报文解析失败: %s body=%s", e, msg.payload[:200])
            return
        log.info("收到平台下行[%s]: %s", msg.topic,
                 json.dumps(data, ensure_ascii=False)[:500])
        try:
            if msg.topic == self.topic_cmd:
                self.handle_command(data)
            elif msg.topic == self.topic_read:
                self.handle_read_property(data)
            elif msg.topic == self.topic_write:
                self.handle_write_property(data)
            else:
                log.warning("未处理主题 %s", msg.topic)
        except Exception as e:
            log.error("下行处理异常: %s", e)

    # ---------- 命令处理 ----------
    def _parse_inputs(self, data):
        """JetLinks 下行 inputs -> {name: value}"""
        out = {}
        inputs = data.get("inputs")
        if isinstance(inputs, list):
            for item in inputs:
                if isinstance(item, dict) and item.get("name") is not None:
                    out[str(item["name"])] = item.get("value")
        return out

    def handle_command(self, data):
        if not isinstance(data, dict):
            return
        cmd_id = data.get("messageId") or data.get("id") or ""
        method = data.get("functionId") or data.get("method") or ""
        args = self._parse_inputs(data)
        if isinstance(data.get("params"), dict):
            args.update(data["params"])
        if not method:
            log.warning("命令缺少 functionId/method: %s",
                        json.dumps(data, ensure_ascii=False)[:200])
            return

        changed = []
        if method == "set_channel":
            try:
                ch = int(args.get("channel"))
            except (TypeError, ValueError):
                self.publish_reply(cmd_id, method, False, "channel 参数非法",
                                   output=False)
                return
            state = args.get("state")
            if state is None or not (1 <= ch <= self.bank.channel_count):
                self.publish_reply(cmd_id, method, False,
                                   f"参数非法 channel={ch} state={state}",
                                   output=False)
                return
            with self.lock:
                if self.bank.set_channel(ch, state):
                    changed = [ch]
                states = self.bank.states[:]
            log.info("set_channel ch%d=%s -> %s", ch, bool(state),
                     self.bank.state_desc(states))
        elif method == "switch_all":
            state = args.get("state")
            if state is None:
                self.publish_reply(cmd_id, method, False, "state 参数缺失",
                                   output=False)
                return
            with self.lock:
                changed = self.bank.set_all_state(state)
                states = self.bank.states[:]
            log.info("switch_all(%s) changed=%s -> %s", bool(state),
                     changed, self.bank.state_desc(states))
        elif method == "write":
            # 兼容旧式 write(r1..r8/chN_state) 命令
            with self.lock:
                changed = self.bank.set_all(**args)
                states = self.bank.states[:]
            log.info("write changed=%s -> %s", changed,
                     self.bank.state_desc(states))
        else:
            log.warning("未知功能 %s", method)
            self.publish_reply(cmd_id, method, False, f"未知功能: {method}",
                               output=False)
            return

        ok = True
        # 状态变化：立即上报 + 每个变化通道触发 switch_change 事件
        if changed:
            self.publish_property()
            for ch in changed:
                with self.lock:
                    on = self.bank.states[ch - 1]
                self.publish_event(ch, on)
        else:
            # 命令到达但状态未变：也回执成功，并刷新一次上报
            self.publish_property()
        self.publish_reply(cmd_id, method, ok,
                           f"{method} 执行完成: {self.bank.state_desc(self.bank.states[:])}",
                           output=True)

    def handle_read_property(self, data):
        """JetMQTT 读属性（读平台关心或全量属性）"""
        ids = data.get("properties") or []
        want = set()
        for it in ids:
            if isinstance(it, str):
                want.add(it)
            elif isinstance(it, dict) and it.get("id"):
                want.add(it["id"])
        with self.lock:
            snap = self.bank.snapshot()
        props = {k: snap[k] for k in snap if not want or k in want}
        mid = data.get("messageId")
        if not mid:
            return
        body = {"productId": self.product_id, "deviceId": self.device_id,
                "messageId": mid, "success": True, "properties": props}
        self.client.publish(self.topic_read_reply, json.dumps(body), qos=1)
        log.info("读属性回复[%s]: %s", self.topic_read_reply,
                 json.dumps({k: v for k, v in props.items()}, ensure_ascii=False)[:400])

    def handle_write_property(self, data):
        """JetMQTT 写属性（支持 chN_state 布尔或 rN 0/1）"""
        props = data.get("properties") or {}
        if not isinstance(props, dict):
            return
        with self.lock:
            changed = self.bank.set_all(**props)
            states = self.bank.states[:]
        if changed:
            self.publish_property()
            for ch in changed:
                self.publish_event(ch, states[ch - 1])
        log.info("写属性 changed=%s -> %s", changed,
                 self.bank.state_desc(states))
        mid = data.get("messageId")
        if mid:
            self.client.publish(self.topic_write_reply,
                                json.dumps({"productId": self.product_id,
                                            "deviceId": self.device_id,
                                            "messageId": mid,
                                            "success": True}), qos=1)

    # ---------- 上报 ----------
    def publish_property(self):
        with self.lock:
            self.bank.tick()
            props = self.bank.snapshot()
            states = self.bank.states[:]
        body = {"productId": self.product_id, "deviceId": self.device_id,
                "timestamp": int(time.time() * 1000), "properties": props}
        self.client.publish(self.topic_property, json.dumps(body), qos=1)
        log.info("属性上报(%s): %s", self.bank.state_desc(states),
                 json.dumps(props, ensure_ascii=False)[:260])

    def publish_event(self, channel, state):
        """事件直发 JetLinks 规范化主题（网关按 /event/{eventId} 识别事件）；
        若先发 /event/post 再经 EMQX 转发，网关通配 /event/+ 也会把原始包当
        eventId=post 解析，产生脏事件记录，因此事件不走 EMQX 规则。"""
        body = {"productId": self.product_id, "deviceId": self.device_id,
                "timestamp": int(time.time() * 1000),
                "eventId": "switch_change",
                "data": {"channel": channel, "state": bool(state)}}
        self.client.publish(self.topic_event, json.dumps(body), qos=1)
        log.info("事件上报[switch_change] ch%d=%s", channel, bool(state))

    def publish_reply(self, cmd_id, method, success, message, output=None):
        # JetMQTT 功能回复仅需 messageId/success/output 三字段
        body = {"messageId": cmd_id, "success": bool(success),
                "output": (True if output is None else bool(output))}
        self.client.publish(self.topic_reply, json.dumps(body, ensure_ascii=False),
                            qos=1)
        log.info("命令响应 -> %s: %s", self.topic_reply,
                 json.dumps(body, ensure_ascii=False)[:300])

    # ---------- 主循环 ----------
    def _auto_loop(self):
        last_report = time.time()
        while not self._stop.wait(0.5):
            now = time.time()
            if now - last_report >= self.interval:
                last_report = now
                if self.client.is_connected():
                    self.publish_property()

    def start(self):
        self.client.connect_async(self._host, self._port, keepalive=60)
        self.client.loop_start()
        threading.Thread(target=self._auto_loop, daemon=True).start()
        log.info("8路继电器模拟器已启动 设备=%s 产品=%s broker=%s:%d 周期=%ss",
                 self.device_id, self.product_id,
                 self._host, self._port, self.interval)

    def stop(self):
        self._stop.set()
        self.client.loop_stop()
        self.client.disconnect()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="8路继电器模拟器(直接运行)")
    parser.add_argument("--device", default=config.DEVICE_ID)
    parser.add_argument("--product", default=config.PRODUCT_ID)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sim = RelaySimulator(args.device, args.product, interval=args.interval)
    try:
        sim.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
