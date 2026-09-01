# MQTT 温湿度模拟终端

基于 MQTT 协议的温湿度数据上报模拟终端，包含两种终端类型：

| 终端 | 文件 | 数据来源 |
|------|------|----------|
| 文件监听终端 | `terminal_file.py` | 手动编辑本地 JSON 文件，保存后立即上报 |
| Modbus 采集终端 | `terminal_modbus.py` | 轮询 Modbus TCP 服务寄存器，数值变化后上报 |

另有辅助工具：`modbus_sim_server.py`（本地 Modbus 模拟服务）、`modbus_write.py`（写入本组寄存器）、`mqtt_monitor.py`（订阅监视）。

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

## 说明

- 两个终端均带 MQTT 自动重连、遗嘱消息（异常掉线时服务器代发 offline）
- 只在数值变化（超过 deadband）时上报，避免无意义刷屏；文件终端另有 5 秒兜底轮询，防止个别编辑器保存方式监听不到
- Modbus 采集终端断线后自动重连 Modbus 服务
