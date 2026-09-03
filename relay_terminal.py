# -*- coding: utf-8 -*-
"""Day3/4 8 路继电器模拟终端（接入 JetLinks 产品 relay8_lfx，设备 RELAY8-TERM-01）

链路设计（参照已完成小组 relay4_mt，本组命名全部带 lfx）：
  模拟器 --property/post--> EMQX 规则 rule_lfx_relay8_property
      -> /relay8_lfx/RELAY8-TERM-01/properties/report  (JetLinks 入库，UI 运行状态可见)
  模拟器 --function/post--> EMQX 规则 rule_lfx_relay8_reply
      -> /relay8_lfx/RELAY8-TERM-01/function/invoke/reply (JetLinks 收命令响应)
  JetLinks --/relay8_lfx/RELAY8-TERM-01/function/invoke--> EMQX 规则 rule_lfx_relay8_cmd
      -> /relay8_lfx/RELAY8-TERM-01/service/cmd           (模拟器收"写继电器"命令)

功能：8 通道状态管理 + 定时上报(默认5s) + 变化立即上报 + MQTT 心跳/自动重连
      + 接收 write 命令（全量写 8 路开关）并回复
运行：python relay_terminal.py [--interval 5]
"""
import argparse
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

import config

log = logging.getLogger("relay-terminal")

CH_COUNT = 8


def _to_bool(v):
    """宽松布尔转换：True/1/'1'/'true'/'on' -> True，其余 -> False"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class RelayChannelBank:
    """8 路继电器状态管理（True=开, False=关）"""

    def __init__(self, initial_all=False):
        self.states = [initial_all] * CH_COUNT

    def set_all(self, **kw):
        """按 {ch1_state: bool, ...} 全量/部分写；返回实际改变的通道列表"""
        changed = []
        for key, val in kw.items():
            if key.startswith("ch") and key.endswith("_state"):
                try:
                    idx = int(key[2:-6]) - 1  # ch1_state -> 0
                except ValueError:
                    continue
                if 0 <= idx < CH_COUNT:
                    val = _to_bool(val)
                    if self.states[idx] != val:
                        self.states[idx] = val
                        changed.append(idx + 1)
        return changed

    def to_report(self):
        """上报字段：deviceId + 时间戳 + ch1_state..ch8_state"""
        payload = {"deviceId": "", "timestamp": int(time.time() * 1000)}
        for i, s in enumerate(self.states, start=1):
            payload[f"ch{i}_state"] = s
        return payload


class RelaySimulator:
    def __init__(self, device_id, product_id, host=None, port=None,
                 username=None, password=None, interval=5.0):
        self.device_id = device_id
        self.product_id = product_id
        self.interval = max(1.0, float(interval))
        self.bank = RelayChannelBank()
        self.lock = threading.Lock()

        self.topic_property = f"/{product_id}/{device_id}/property/post"
        self.topic_reply = f"/{product_id}/{device_id}/function/post"
        self.topic_cmd = f"/{product_id}/{device_id}/service/cmd"

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=device_id, clean_session=True, protocol=mqtt.MQTTv311)
        self.client.username_pw_set(username or "test", password or "123456")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # paho 自带自动重连（1s 起步指数退避）
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.will_set(f"/{product_id}/{device_id}/offline",
                             json.dumps({"deviceId": device_id}), qos=1)
        self._host = host or config.MQTT_HOST
        self._port = port or config.MQTT_PORT
        self._stop = threading.Event()

    # ---------- MQTT 回调 ----------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("已连接 MQTT %s:%d, 订阅命令 %s",
                     self._host, self._port, self.topic_cmd)
            client.subscribe(self.topic_cmd, qos=1)
            # 上线后立即上报一次，平台据此置设备 online
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
        log.info("收到平台下行命令: %s", json.dumps(data, ensure_ascii=False))
        self.handle_command(data)

    # ---------- 业务 ----------
    def _extract_command(self, data):
        """兼容两种下行报文（返回 cmd_id, method, writes字典）：

        A. JetLinks 原始 function/invoke（EMQX 规则纯透传后到达 /service/cmd）：
           {messageId, functionId:"write", inputs:[{name:"r1",value:1},...]}
           inputs 项 name 也可能是 "params"，value 为 {r1:..,r8:..}
        B. /service/cmd 标准格式：
           {id, method:"write", params:{ch1_state:true,...}}
        """
        cmd_id = data.get("messageId") or data.get("id") or ""
        method = data.get("functionId") or data.get("method") or ""
        writes = {}
        inputs = data.get("inputs")
        if isinstance(inputs, list):
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                val = item.get("value")
                if name == "params" and isinstance(val, dict):
                    writes.update(val)
                elif name:
                    writes[name] = val
        params = data.get("params")
        if isinstance(params, dict):
            writes.update(params)
        return cmd_id, method, writes

    def handle_command(self, data):
        cmd_id, method, writes = self._extract_command(data)
        if not method:
            log.warning("命令缺少 method/functionId: %s",
                        json.dumps(data, ensure_ascii=False)[:200])
            return
        if method == "write":
            kw = {}
            for k, v in writes.items():
                if k.startswith("r") and k[1:].isdigit():
                    idx = int(k[1:])
                    if 1 <= idx <= CH_COUNT:
                        kw[f"ch{idx}_state"] = v
                elif k.startswith("ch") and k.endswith("_state"):
                    kw[k] = v
            with self.lock:
                changed = self.bank.set_all(**kw)
                states = self.bank.states[:]
            if changed:
                log.info("写继电器执行: 通道%s 已切换 -> %s",
                         changed, self.state_desc(states))
            else:
                log.info("写继电器执行: 状态无变化 -> %s", self.state_desc(states))
            # 1) 上报最新属性（变化立即上报，UI 刷新）
            self.publish_property()
            # 2) 回复执行结果（平台显示"成功"）
            self.publish_reply(cmd_id, "write", True,
                               f"OK 已写入 8 路: {self.state_desc(states)}")
        else:
            log.warning("未知方法 %s", method)
            self.publish_reply(cmd_id, method, False, f"未知功能: {method}")

    def publish_property(self):
        with self.lock:
            payload = self.bank.to_report()
        payload["deviceId"] = self.device_id
        body = json.dumps(payload, ensure_ascii=False)
        self.client.publish(self.topic_property, body, qos=1)
        log.info("属性上报 -> %s: %s", self.topic_property,
                 json.dumps({k: v for k, v in payload.items()
                             if k not in ("timestamp", "deviceId")}, ensure_ascii=False))

    def publish_reply(self, cmd_id, method, success, message):
        payload = {"deviceId": self.device_id, "id": cmd_id,
                   "success": bool(success), "method": method,
                   "message": message}
        body = json.dumps(payload, ensure_ascii=False)
        self.client.publish(self.topic_reply, body, qos=1)
        log.info("命令响应 -> %s: %s", self.topic_reply,
                 json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def state_desc(states):
        return " ".join(f"{i}={1 if s else 0}" for i, s in enumerate(states, 1))

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


def main():
    parser = argparse.ArgumentParser(description="8路继电器模拟终端(Day3/4)")
    parser.add_argument("--device", default="RELAY8-TERM-01")
    parser.add_argument("--product", default="relay8_lfx")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--passwd", default=None)
    parser.add_argument("--interval", type=float, default=5.0, help="定时上报秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sim = RelaySimulator(args.device, args.product, args.host, args.port,
                         args.user, args.passwd, args.interval)
    try:
        sim.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到退出信号")
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
