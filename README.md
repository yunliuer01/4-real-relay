# MQTT 温湿度模拟终端

基于 MQTT 协议的温湿度数据上报模拟终端，包含多种终端类型。

另有辅助工具：`gui_sensor.py`（GUI 温湿度模拟器）、`jetlinks_terminal.py`（JetLinks 平台接入，含两种适配模式与下行控制）、`jetlinks_rule.sql`（EMQX 规则转换 SQL）、`modbus_sim_server.py`（本地 Modbus 模拟服务）、`modbus_write.py`（写入本组寄存器）、`mqtt_monitor.py`（订阅监视）。

## GUI 温湿度模拟器（推荐）

双击 `start_gui.bat` 启动（或命令行运行 `python gui_sensor.py`）。拖动滑块调整温湿度，松手即自动上报 MQTT 并同步写入 `data/sensor_data.json`，界面日志实时显示服务器回传的消息，无需手动编辑文件。

| 终端 | 文件 | 数据来源 |
|------|------|----------|
| **GUI 模拟器** | `gui_sensor.py` | 图形界面拖滑块设置数值，松手即上报 |
| 文件监听终端 | `terminal_file.py` | 手动编辑本地 JSON 文件，保存后立即上报 |
| Modbus 采集终端 | `terminal_modbus.py` | 轮询 Modbus TCP 服务寄存器，数值变化后上报 |

> GUI 模拟器与文件监听终端功能等价（内置上报，无需同时运行 `terminal_file.py`）；参数：`--device 设备ID`。

## MQTT 服务器

配置在 `config.py`：`172.16.4.211:9783`，账号 `test`，密码 `123456`。
命令行可用 `--host` / `--port` 临时覆盖。

## 上报格式

- 数据主题：`terminal/{device_id}/th`，QoS 1
- 状态主题：`terminal/{device_id}/status`（遗嘱消息：online / offline，保留消息）

```json
{
  "device_id": "MODBUS-TERM-01",
  "type": "modbus",
  "temperature": 25.5,
  "humidity": 60.2,
  "timestamp": "2026-09-01T10:00:00"
}
```

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

### 1. 文件监听终端

```bash
python terminal_file.py
```

默认监听 `data/sensor_data.json`。手动修改其中数值并保存即可触发上报：

```json
{
  "temperature": 26.8,
  "humidity": 55.0
}
```

常用参数：
- `--file 路径` 指定数据文件
- `--device-id FILE-TERM-01` 设备 ID（同时决定上报主题）
- `--deadband 0.01` 变化阈值

### 2. Modbus 采集终端

默认连接实验平台从站 **192.168.20.59:5502**，本组寄存器 **0x0005**。

寄存器编码约定（16 位）：**高 8 位 = 温度℃，低 8 位 = 湿度%RH**，
例如寄存器值 `0x1F3D`（2597）解析为 温度 31℃ / 湿度 61%RH。

```bash
# 直接连接实验平台（默认参数即本组配置）
python terminal_modbus.py

# 连接本仓库自带的模拟服务做本地测试
python modbus_sim_server.py                                            # 窗口1：启动模拟服务(端口5020)
python terminal_modbus.py --modbus-host 127.0.0.1 --modbus-port 5020   # 窗口2：采集终端
```

常用参数：
- `--reg 0x0005` 本组保持寄存器地址（实验平台 0x0000~0x0009，各组独立避免冲突；地址超出范围会告警）
- `--modbus-host 192.168.20.59 --modbus-port 5502` 从站地址端口
- `--slave-id 1` 从站 ID
- `--interval 2` 轮询周期（秒）
- `--deadband 1` 变化阈值（默认 1℃ / 1%RH），超过才上报

### 3. 写入本组寄存器（演示/测试用）

```bash
# 把温湿度打包写入实验平台本组寄存器 0x0005，采集终端会检测到变化并上报
python modbus_write.py --temp 26 --hum 58
```

### 4. 查看上报数据

```bash
python mqtt_monitor.py                # 订阅 terminal/#，实时打印
```

### 5. JetLinks 平台接入（EMQX → JetLinks 双适配）

JetLinks 网页：`http://172.16.4.211:9000`（MQTT 接入端口仍为 9783）。

**平台账号（本小组）**

| 平台 | 账号 | 用途 |
|------|------|------|
| JetLinks 网页 | `admin5` / `Admin@group5` | 浏览器登录 9000 端口，建产品/物模型/设备 |
| EMQX Dashboard | `group5` / `Admin@group5` | 管理端账号（18083 未开放，预留） |
| MQTT 设备接入 | `test` / `123456` | 终端连接 9783 用（实测有效，勿用 group5） |

#### 5.1 JetLinks 网页配置（一次性）

1. **登录**（admin5/Admin@group5）JetLinks → 左侧 **设备管理 → 产品** → 新增产品：
   - 产品名称 `MQTT温湿度终端`，产品ID 填 **`mqtt-iot`**，消息协议选 **JetLinks 官方协议**，网络协议 MQTT
2. 进入产品 → **物模型**：
   - 属性 `temperature`（数值型，标识 temperature，读写类型只读）
   - 属性 `humidity`（数值型，标识 humidity，只读）
   - 功能 `setInterval`（参数 `interval` 整数，用于设置上报间隔秒数）
3. **设备管理 → 设备** → 新增设备：设备ID 填 **`FILE-TERM-01`**，所属产品选 `mqtt-iot`，保存后记录设备状态为"启用"
4. 设备接入 EMQX 使用现有账号（test/123456）；JetLinks 通过主题中的产品ID/设备ID识别设备

#### 5.2 两种适配路线

**路线 A（不改终端，EMQX 规则转换）**：终端保持原始报文 `terminal/{deviceId}/th`，
由 EMQX 规则引擎转换为 JetLinks 物模型格式并转发到 `/mqtt-iot/{deviceId}/properties/report`。
规则 SQL 见 `jetlinks_rule.sql`（EMQX Dashboard → 规则引擎 → 创建规则，粘贴 SQL + Republish 动作）。

```bash
python jetlinks_terminal.py --mode emqx     # 终端侧只需加 --mode emqx
```

**路线 B（终端直接适配 JetLinks 报文）**：终端直接上报 `{"properties":{...}}` 到
`/mqtt-iot/FILE-TERM-01/properties/report`，无需 EMQX 规则。

```bash
python jetlinks_terminal.py                 # 默认 jetlinks 模式
```

#### 5.3 平台下发控制（最终目标）

终端已订阅 JetLinks 下行主题，JetLinks 网页 **设备详情 → 功能调用** 点按钮即可控制：

| 平台操作 | 终端行为 |
|---|---|
| 功能调用 `setInterval(interval=3)` | 上报间隔改为 3 秒，回复 `{messageId, success, output}` |
| 读取属性 | 回复当前温湿度 `{messageId, properties, success}` |
| 修改属性 `temperature=30` | 写入本地 JSON 文件并回复，文件变化自动触发上报 |

已验证的 MQTT 协议闭环（JetLinks 平台侧接入后即可直接操作）：
```
读取属性  -> /mqtt-iot/JL-TEST-01/properties/read/reply  {"messageId":"m-read-1","properties":{"temperature":26.3,...},"success":true}
功能调用  -> /mqtt-iot/JL-TEST-01/function/invoke/reply   {"messageId":"m-fn-1","success":true,"output":"上报间隔已设置为 3 秒"}
修改属性  -> /mqtt-iot/JL-TEST-01/properties/write/reply  {"messageId":"m-wr-1","success":true,"properties":{"temperature":30.0}}
```

> 注意：JetLinks 协议主题带前导斜杠（`/mqtt-iot/FILE-TERM-01/...`），与 `terminal/...` 格式不同。
> EMQX Dashboard（18083）若无法访问，规则引擎需联系老师开通。

## 说明

- 两个终端均带 MQTT 自动重连、遗嘱消息（异常掉线时服务器代发 offline）
- 只在数值变化（超过 deadband）时上报，避免无意义刷屏；文件终端另有 5 秒兜底轮询，防止个别编辑器保存方式监听不到
- Modbus 采集终端断线后自动重连 Modbus 服务
