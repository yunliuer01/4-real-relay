# 4 路继电器 + Modbus TCP/RTU 采集网关（MicroPython / ESP32-C3）

固件文件：
- `main.py`：主程序（配网、WiFi、MQTT、继电器、Modbus 线程调度）
- `modbus_tcp_master.py`：Modbus TCP 主站，独立线程运行，不阻塞继电器
- `modbus_master.py`：Modbus RTU 主站（保留，mode=rtu 时启用）
- `portal_page.html`：AP 配网 Web 页面（部署到设备上为 `portal.html`）

## 功能特性

- **配网模式**：长按 SW1 按键 5 秒，设备开放热点 `Relay4-Setuplfx`（IP `192.168.4.1`），用手机/电脑连接后打开 `http://192.168.4.1` 填写参数并保存，设备自动重启联网。
- **参数持久化**：配置保存到板载 flash `/config.json`，掉电不丢失。
- **正常运行模式**：连上 WiFi 后自动连接 MQTT，按 JetLinks 协议上报属性、事件、命令回复；支持断线自动重连。
- **4 路继电器控制**：支持 `set_channel`（单路）、`switch_all`（全路），状态变化触发 `switch_change` 事件。
- **Modbus TCP 采集网关**：
  - 作为 Modbus TCP 客户端，采集多个服务器/从站的保持/输入寄存器
  - 每个从站独立配置 `host` / `port` / `unit_id`
  - 每个从站可挂多个寄存器，独立配置采集周期、上报 key、上报产品、可写标志
  - 采集线程独立运行，不阻塞继电器控制与 MQTT 命令响应
  - 采集结果自动合并到属性上报中，支持 `scale`、`signed`、`digits` 等转换
  - 支持通过 MQTT / HTTP 写单个保持寄存器（功能码 06）
- **双 Topic 模式**：
  - `direct`（默认）：沿用 EMQX 规则路径，如 `/{productId}/{deviceId}/property/post`
  - `sys`：JetLinks MQTT 网关规范路径，如 `/sys/{productId}/{deviceId}/thing/event/property/post`

## 硬件接线（默认）

在 `main.py` 顶部修改：

```python
RELAY_PINS = [3, 4, 5, 7]                    # 4 路继电器 GPIO，低电平吸合（RELAY1~4）
SW1_PIN = 10                                 # 配网按键 SW1 = IO10，上拉输入，按下为低电平
LED_PIN = 2                                  # 状态指示灯 GPIO（IO2）
```

Modbus RTU（UART1）默认接线：

```python
"modbus": {
    "enabled": True,
    "uart_id": 1,
    "baudrate": 9600,
    "tx_pin": 20,        # ESP32 UART1 TX
    "rx_pin": 21,        # ESP32 UART1 RX
    "dir_pin": 8,        # RS485 DE/RE 方向控制，None 表示自动方向模块
    "timeout_ms": 500,
    "retries": 2
}
```

> **注意**：默认引脚匹配 CORE-ESP32-C3 四路继电器板背面丝印：RELAY1/IO3、RELAY2/IO4、RELAY3/IO5、RELAY4/IO7、SW1/IO10、LED/IO2。ESP32-C3 上 14-19 通常不可用（Flash/USB 专用），0/2/8/9 为 strapping 引脚，使用时应谨慎。

## 刷写步骤

已提供 MicroPython 固件：`esp32-relay4-modbus-gateway/LOLIN_C3_MINI-20241025-v1.24.0.bin`（ESP32-C3 v1.24.0，完整 Factory 镜像，需写入 0x0）。

> 注意：`_mpy_c3_v1.24.0.bin` 是仅包含应用分区的 `.app-bin`，不能直接写入 0x0，否则会出现 `No bootable app partitions` 启动错误。

### 一键刷写（推荐）

用 USB 连接板子，确定串口号（设备管理器 → 端口），在仓库根目录执行：

```powershell
D:\8-relay\esp32-relay4-modbus-gateway\flash_esp32.bat COMx
```

示例：

```powershell
D:\8-relay\esp32-relay4-modbus-gateway\flash_esp32.bat COM3
```

脚本会自动：擦除 Flash → 写入 MicroPython 固件 → 上传 `modbus_master.py` → 上传 `main.py`。

### 手动刷写

```powershell
# 1. 擦除 Flash（若无法自动进下载模式，先按住 BOOT 再按 RST，然后松开 BOOT）
python -m esptool --port COMx --chip esp32-c3 erase_flash

# 2. 写入 MicroPython 固件
python -m esptool --port COMx --chip esp32-c3 --baud 460800 write_flash -z 0x0 "D:\8-relay\esp32-relay4-modbus-gateway\LOLIN_C3_MINI-20241025-v1.24.0.bin"

# 3. 上传 modbus_tcp_master.py
python -m mpremote connect COMx fs cp "D:\8-relay\esp32-relay4-modbus-gateway\modbus_tcp_master.py" :modbus_tcp_master.py

# 4. 上传 modbus_master.py（保留 RTU 模式回退）
python -m mpremote connect COMx fs cp "D:\8-relay\esp32-relay4-modbus-gateway\modbus_master.py" :modbus_master.py

# 5. 上传 main.py
python -m mpremote connect COMx fs cp "D:\8-relay\esp32-relay4-modbus-gateway\main.py" :main.py

# 6. 上传 portal.html（配网页面）
python -m mpremote connect COMx fs cp "D:\8-relay\esp32-relay4-modbus-gateway\portal_page.html" :portal.html
```

5. **按 RST 复位**，串口输出日志即开始运行。

## 首次使用流程

1. 上电后若没有配置或 WiFi 连接失败，自动进入配网模式：
   - 串口打印：`配网模式: 连接热点 Relay4-Setuplfx，打开 http://192.168.4.1`
   - 手机/电脑连 `Relay4-Setuplfx`（开放无密码），浏览器打开 `http://192.168.4.1`。
2. 在网页中填写：
   - WiFi 名称 / 密码（2.4GHz）
   - MQTT 服务器地址 / 端口 / 账号 / 密码
   - 产品 ID（默认 `relay4_lfx`）
   - 设备 ID（默认读取 MAC 地址，可修改）
   - 主题模式：选 `direct`（对接现有 EMQX 规则）或 `sys`
- 上报周期（默认 5 秒）
  - **Modbus TCP 配置**：在 Web 页面直接添加从站、寄存器，自动保存为 JSON
3. 点击保存 → 设备写入 `/config.json` 并自动重启。
4. 设备连上 WiFi 和 MQTT 后，在 JetLinks 平台即可看到设备上线、属性上报、命令执行。

## Modbus TCP 配置示例

在配网页面 "Modbus TCP 采集网关配置" 中配置：

```json
{
  "enabled": true,
  "mode": "tcp",
  "timeout_ms": 500,
  "retries": 2,
  "retry_interval_ms": 500,
  "slaves": [
    {
      "enabled": true,
      "host": "192.168.20.59",
      "port": 5502,
      "unit_id": 4,
      "registers": [
        {"addr": 3, "func": 3, "key": "temperature", "product": "th-lfx", "period_ms": 2000, "scale": 1, "signed": false, "digits": 0, "writable": true},
        {"addr": 4, "func": 3, "key": "humidity", "product": "th-lfx", "period_ms": 2000, "scale": 1, "signed": false, "digits": 0, "writable": true}
      ]
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `enabled` | 是否启用 Modbus 采集 |
| `mode` | `"tcp"` 或 `"rtu"` |
| `timeout_ms` | 单帧等待超时 |
| `retries` | 失败重试次数 |
| `retry_interval_ms` | 重试间隔 |
| `slaves` | 从站列表 |
| `host` / `port` / `unit_id` | Modbus TCP 服务器 IP、端口、单元 ID |
| `registers` | 该从站要采集的寄存器列表 |
| `addr` | 寄存器地址（十进制，如 0 对应 0x0000） |
| `func` | 功能码（3=保持寄存器，4=输入寄存器） |
| `key` | 上报到 JetLinks 的 JSON 属性名 |
| `product` | 上报产品标识（仅用于虚拟设备桥接标注；只填本组自己的产品，如 `th-lfx`，留空则采集值随本网关 `relay4_lfx` 一并上报。不要填其他组的产品） |
| `scale` | 原始值缩放系数 |
| `period_ms` | 该寄存器采集周期 |
| `signed` | 是否按有符号 16 位解析 |
| `digits` | 结果保留小数位数 |
| `writable` | 是否允许平台写回该寄存器 |

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

在正常模式下，**长按 SW1 5 秒**，设备会关闭继电器、断开 MQTT/WiFi/Modbus，重新打开 `Relay4-Setuplfx` 热点。

## 调试提示

- 串口打印是最直接的调试方式：在 Thonny Shell 或任意串口工具 115200 波特率查看。
- 若 MQTT 连不上，先确认 MQTT 账号/密码、端口，以及 `topic_mode` 是否与平台/规则匹配。
- 若 WiFi 连不上，检查是否使用 5GHz 热点（ESP32-C3 只支持 2.4GHz）。
- 想清除配置重新配网：删除板子里的 `/config.json` 或填错 WiFi 导致 30 秒超时后自动回配网模式。
- Modbus 采集日志以 `[MODBUS]` 前缀输出，包含每次轮询的从站、地址、原始值、转换后的值，便于现场排障。

## 已知限制

- 电压/电流/功率为占位值（真实硬件如无传感器则按固定负载模拟）。
- 若多个从站配置了相同的 `key`，属性上报中会发生覆盖，请确保同一产品下属性名唯一或加入从站前缀区分。
