# -*- coding: utf-8 -*-
"""共享 MQTT 客户端封装：连接、自动重连、遗嘱消息、数据上报"""
import json
import logging
import ssl
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

import config

log = logging.getLogger("mqtt")


class MqttReporter:
    """封装 paho-mqtt，提供连接管理与温湿度数据上报"""

    def __init__(self, device_id: str, device_type: str,
                 host: str = None, port: int = None,
                 username: str = None, password: str = None):
        self.device_id = device_id
        self.device_type = device_type
        self.host = host or config.MQTT_HOST
        self.port = port or config.MQTT_PORT
        self.username = username or config.MQTT_USER
        self.password = password or config.MQTT_PASS

        # client_id 加随机后缀，避免多终端相互挤掉连接
        client_id = f"{device_id}-{uuid.uuid4().hex[:6]}"
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        # 遗嘱消息：终端异常掉线时，服务器代发 offline
        self.client.will_set(
            config.status_topic(device_id), "offline", qos=1, retain=True
        )

        self._connected = False

    # ---------- 回调 ----------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected = True
            client.publish(config.status_topic(self.device_id),
                           "online", qos=1, retain=True)
            log.info("已连接 MQTT 服务器 %s:%s", self.host, self.port)
        else:
            log.error("MQTT 连接失败: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        if reason_code != 0:
            log.warning("MQTT 连接断开 (code=%s)，将自动重连...", reason_code)

    # ---------- 对外接口 ----------
    def start(self):
        """建立连接并启动后台网络线程（内置自动重连）"""
        self.client.connect_async(self.host, self.port, config.MQTT_KEEPALIVE)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.publish(config.status_topic(self.device_id),
                                "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        log.info("MQTT 客户端已停止")

    def report(self, temperature: float, humidity: float) -> bool:
        """上报一条温湿度数据，返回是否成功提交"""
        payload = {
            "device_id": self.device_id,
            "type": self.device_type,
            "temperature": round(float(temperature), 2),
            "humidity": round(float(humidity), 2),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        topic = config.data_topic(self.device_id)
        info = self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("上报 -> %s  温度=%.2f℃  湿度=%.2f%%RH", topic, temperature, humidity)
            return True
        if info.rc == mqtt.MQTT_ERR_NO_CONN:
            log.warning("尚未连接服务器，消息进入队列，连上后自动补发")
            return True
        log.error("上报失败 rc=%s", info.rc)
        return False

    @property
    def connected(self) -> bool:
        return self._connected
