# -*- coding: utf-8 -*-
"""全局配置：MQTT 服务器连接信息与上报格式"""

# ---- MQTT 服务器 ----
MQTT_HOST = "172.16.4.211"
MQTT_PORT = 9783
MQTT_USER = "test"
MQTT_PASS = "123456"
MQTT_KEEPALIVE = 60  # 秒

# ---- 上报主题 ----
# 数据主题：terminal/{device_id}/th
# 在线状态主题（遗嘱消息）：terminal/{device_id}/status
def data_topic(device_id: str) -> str:
    return f"terminal/{device_id}/th"


def status_topic(device_id: str) -> str:
    return f"terminal/{device_id}/status"


# ---- 上报数据格式 ----
# {"device_id": "...", "type": "file|modbus", "temperature": 25.3,
#  "humidity": 60.5, "timestamp": "2026-09-01T10:00:00"}
