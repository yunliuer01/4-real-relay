# -*- coding: utf-8 -*-
"""8 路继电器项目全局配置：MQTT / JetLinks / EMQX 连接信息与产品设备 ID"""

# ---- MQTT 服务器（设备接入）----
MQTT_HOST = "172.16.4.211"
MQTT_PORT = 9783
MQTT_USER = "test"          # 设备接入账号（实测有效；group5 账号不能用于 MQTT 接入）
MQTT_PASS = "123456"
MQTT_KEEPALIVE = 60  # 秒

# ---- JetLinks 平台（管理端网页/API）----
# 网页登录：http://172.16.4.211:9000
JETLINKS_API = "http://172.16.4.211:9000/api"
JETLINKS_WEB_USER = "admin5"
JETLINKS_WEB_PASS = "Admin@group5"

# ---- EMQX Dashboard API（规则管理）----
EMQX_API = "http://172.16.4.211:9183/api/v5"
EMQX_ADMIN_USER = "group5"
EMQX_ADMIN_PASS = "Admin@group5"

# ---- 8 路继电器产品 / 设备 ----
PRODUCT_ID = "relay8_lfx"
DEVICE_ID = "RELAY8-TERM-01"
RELAY_CHANNELS = 8

# ---- JetLinks mqtt 接入网关（平台内置 demo，各小组通用）----
MQTT_PROTOCOL_ID = "2092561848730316800"     # mqtt 协议
MQTT_ACCESS_ID = "2092562181967769600"       # mqtt接入 网关
