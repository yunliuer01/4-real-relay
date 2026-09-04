# 8 路继电器真实设备固件（MicroPython / ESP32-C3）

固件文件：`main.py`

## 功能特性

- **配网模式**：长按 SW1 按键 5 秒，设备开放热点 `Relay8-Setup`（IP `192.168.4.1`），用手机/电脑连接后打开 `http://192.168.4.1` 填写参数并保存，设备自动重启联网。
- **参数持久化**：配置保存到板载 flash `/config.json`，掉电不丢失。
- **正常运行模式**：连上 WiFi 后自动连接 MQTT，按 JetLinks 协议上报 34 项属性、事件、命令回复；支持断线自动重连。
- **8 路继电器控制**：支持 `set_channel`（单路）、`switch_all`（全路），状态变化触发 `switch_change` 事件。
- **双 Topic 模式**：
  - `direct`（默认）：沿用 Day3/Day4 的 EMQX 规则路径，如 `/{productId}/{deviceId}/property/post`
  - `sys`：JetLinks MQTT 网关规范路径，如 `/sys/{productId}/{deviceId}/thing/event/property/post`

## 硬件接线（默认）

在 `main.py` 顶部修改：

```python
RELAY_PINS = [3, 4, 5, 7, 10, 18, 19, 20]   # 8 路继电器 GPIO，低电平吸合
SW1_PIN = 8                                  # 配网按键 GPIO，上拉输入，按下为低电平
LED_PIN = None                               # 状态指示灯 GPIO（不需要填 None）
```

> **注意**：默认引脚基于常见的 CORE-ESP32-C3 8 路继电器板，请根据你的真实原理图核对并修改。不要使用 strapping/Flash 专用引脚驱动继电器，否则可能无法正常启动。

## 刷写步骤

1. **擦除并刷 MicroPython 固件**（已提供 `固件/LOLIN_C3_MINI-20241025-v1.24.0.bin`）：

```powershell
python -m esptool --port COMx erase_flash
python -m esptool --port COMx write_flash -z 0x0 "d:/esp32_test/MicroPython开发/固件/LOLIN_C3_MINI-20241025-v1.24.0.bin"
```

2. **用 Thonny 连接板子**，打开 `main.py`，选择 **文件 → 另存为 → MicroPython 设备**，命名为 `main.py`。

3. **按 RST/BOOT 复位**，串口输出日志即开始运行。

## 首次使用流程

1. 上电后若没有配置或 WiFi 连接失败，自动进入配网模式：
   - 串口打印：`配网模式: 连接热点 Relay8-Setup，打开 http://192.168.4.1`
   - 手机连 `Relay8-Setup`（开放无密码），浏览器打开 `http://192.168.4.1`。
2. 在网页中填写：
   - WiFi 名称 / 密码（2.4GHz）
   - MQTT 服务器地址 / 端口 / 账号 / 密码
   - 产品 ID（默认 `relay8_lfx`）
   - 设备 ID（默认读取 MAC 地址，可修改）
   - 主题模式：选 `direct`（对接现有 EMQX 规则）或 `sys`
   - 上报周期（默认 5 秒）
3. 点击保存 → 设备写入 `/config.json` 并自动重启。
4. 设备连上 WiFi 和 MQTT 后，在 JetLinks 平台即可看到设备上线、属性上报、命令执行。

## MQTT Topic 说明

| 方向 | direct 模式 | sys 模式 |
|------|------------|----------|
| 属性上报 | `/{productId}/{deviceId}/property/post` | `/sys/{productId}/{deviceId}/thing/event/property/post` |
| 命令/功能调用 | `/{productId}/{deviceId}/service/cmd` | `/sys/{productId}/{deviceId}/thing/service/{functionId}/invoke` |
| 命令回复 | `/{productId}/{deviceId}/function/post` | `/sys/{productId}/{deviceId}/thing/service/{functionId}/invoke/reply` |
| 事件上报 | `/{productId}/{deviceId}/event/switch_change` | `/sys/{productId}/{deviceId}/thing/event/switch_change` |
| 上线 | `/{productId}/{deviceId}/online` | `/sys/{productId}/{deviceId}/online` |
| 离线 | `/{productId}/{deviceId}/offline` | `/sys/{productId}/{deviceId}/offline` |

## 命令示例

`set_channel`：

```json
{
  "functionId": "set_channel",
  "messageId": "uuid-001",
  "inputs": [
    {"name": "channel", "value": 3},
    {"name": "state", "value": true}
  ]
}
```

`switch_all`：

```json
{
  "functionId": "switch_all",
  "messageId": "uuid-002",
  "inputs": [
    {"name": "state", "value": true}
  ]
}
```

回复：

```json
{"messageId": "uuid-001", "success": true, "output": true}
```

## 重新配网

在正常模式下，**长按 SW1 5 秒**，设备会关闭继电器、断开 MQTT/WiFi，重新打开 `Relay8-Setup` 热点。

## 调试提示

- 串口打印是最直接的调试方式：在 Thonny Shell 或任意串口工具 115200 波特率查看。
- 若 MQTT 连不上，先确认 MQTT 账号/密码、端口，以及 `topic_mode` 是否与平台/规则匹配。
- 若 WiFi 连不上，检查是否使用 5GHz 热点（ESP32-C3 只支持 2.4GHz）。
- 想清除配置重新配网：删除板子里的 `/config.json` 或填错 WiFi 导致 30 秒超时后自动回配网模式。

## 已知限制

- 电压/电流/功率为占位值（真实硬件如无传感器则按固定负载模拟）。
- 温度/湿度为占位值，未接真实传感器；如需真实值，可替换 `ChannelModel` 中的读取逻辑。
