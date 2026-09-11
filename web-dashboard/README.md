# web-dashboard

ESP32 4 路继电器网关的配套 **Web 端监控与配置后台**，对应 `7ce8b1c1a7fc` 父网关
与 4 个继电器子设备。

> 前端只包含 HTML/CSS/JS 静态资源，**不含后端**。后端 Python Flask 服务见
> `mqtt-iot-terminal` 仓库（`web/app.py`，paho-mqtt 后台订阅 + SSE 桥接）。
> 这里只把前端这份独立 UI 上传至此仓库，便于在 ESP32 项目仓库里一并浏览。

## 文件结构

```
web-dashboard/
├── README.md         # 本文件
├── dashboard.html    # 全链路监控大屏（3 页：实时监控 / 数据分析 / 场景联动）
├── index.html        # 设备映射 / JetLinks 平台设备管理后台
├── mapping.json      # 继电器通道与功能 / 子设备的映射配置
└── vendor/           # 本地化的 Vue 3 + ECharts 5（避免 CDN 302/MIME 风险）
    ├── vue.global.js   # 594 KB
    └── echarts.min.js  # 1.0 MB
```

## 部署

任选其一即可：

### A. 直接双击打开（前端单机浏览，无后端）

打开 `dashboard.html` 即可看到静态样式 + 占位数据。浏览器连不到 `/api/live`
（无后端），实时数据不会刷新，但页面布局、滚动、按钮交互都正常。

### B. 走 `mqtt-iot-terminal` 后端

```bash
git clone <mqtt-iot-terminal-url>
cd mqtt-iot-terminal
python web/app.py     # 启动 Flask + paho-mqtt 后台订阅
# 浏览器打开 http://localhost:5000/dashboard
```

把本目录的 `dashboard.html` / `index.html` / `mapping.json` / `vendor/` 拷到
`mqtt-iot-terminal/web/static/` 覆盖即可更新前端。

## 版本对齐

| web-dashboard 版本         | 对应 ESP32 固件版本                | 对应 bridge 版本      |
|----------------------------|------------------------------------|------------------------|
| `v6.0.6-dashboard-upload`  | `v6.0.6-bridge-th-mapping-fix`     | `v6.0.6-bridge-...`    |
| `v3.5-dashboard-all-lights-fix` | `v6.0.5-http-poll-freeze-fix` | （沿用）              |

> ESP32 固件 `main.py` 版本 `v6.0.5-http-poll-freeze-fix` 已稳定运行；前端与固件
> 是解耦的（HTTP API + MQTT topic），单独升级任一边不必同步另一边。

## dashboard 大屏特性

- **3 页分屏**：滚动翻页 — 实时监控 / 数据分析 / 场景联动
- **实时数据**：通过 `/api/live` SSE 接收 MQTT 桥接的设备状态
- **告警对齐**：以 JetLinks 平台 `alarm` 字段为准，烟雾浓度只展示不告警
- **场景联动**：11 个独立按钮 — 门锁 解锁/上锁、灯1 开/关、灯2 开/关、空调 开/关、
  全开照明（仅 CH2+CH3）、全关照明（仅 CH2+CH3）、告警复位
- **离线缓存**：无后端时仍能打开，状态保持最后一次刷新

## 协议约定

- 上行：`/relay4_lfx/7ce8b1c1a7fc/property/post`、`/+/+/properties/report`
- 下行（通过 HTTP 轮询）：`http://192.168.30.160/api/relay?ch={1..4}&state={0|1}`

详细见 8-relay 仓库根 README 与 `mqtt-iot-terminal` 仓库。