# 虚拟设备映射桥接器 (Virtual Device Bridge)

## 1. 解决什么问题？

原来的 4 路继电器/Modbus 网关作为一个**父设备**上报，报文里同时包含：

- 4 路继电器开关状态、电压、电流、功率
- Modbus 采集到的温湿度等传感器值
- 设备自身心跳/在线状态

JetLinks/EMQX 的规则引擎要把这一包数据拆给**不同产品**的多个子设备（门锁、灯、空调、温湿度传感器等），规则会变得越来越复杂、难以维护。

这个桥接器就是一个**独立的、可编程的消息转发服务**：

- 订阅父网关的所有上行消息；
- 按 `config.yaml` 里的映射规则，拆成多个子设备属性/事件；
- 发布到对应子设备的 MQTT topic；
- 同时监听各子设备的下行命令，聚合回父网关的控制帧。

功能上等价于 EMQX 规则引擎，但用 Python 代码实现，规则可以随意扩展。

## 2. 目录结构

```
virtual-device-bridge/
├── bridge.py           # 桥接器主程序
├── config.yaml         # 虚拟设备映射配置
├── requirements.txt    # 依赖
└── README.md           # 本文档
```

## 3. 配置说明 (config.yaml)

### 3.1 MQTT 与父网关

```yaml
mqtt:
  broker: "172.16.4.211"
  port: 9783
  username: "test"
  password: "123456"
  client_id: "virtual-bridge-01"

gateway:
  product_id: "relay4-modbus-gateway"
  device_id: "7ce8b1c1a7fc"
```

### 3.2 继电器 → 虚拟设备

示例：把通道 1 映射成“门锁”。

```yaml
relays:
  - channel: 1
    product_id: "virtual-lock"
    device_id: "lock-001"
    name: "门锁"
    property_map:
      ch1_state: lockState          # 父属性 -> 子属性
    event_map:
      switch_change:
        event_id: "unlockRecord"
        data_map:
          channel: sourceChannel
          state: lockState
    command_map:
      setLock:
        function_id: "set_channel"
        inputs:
          channel: 1
          state: "{value}"
```

上行时，父设备 `properties.ch1_state` 会被拆成 `lock-001.properties.lockState` 上报。

下行时，子设备 `lock-001/service/cmd` 收到 `setLock` 命令后，会转成父网关命令：

```json
{
  "functionId": "set_channel",
  "inputs": [{"name":"channel","value":1}, {"name":"state","value":true}]
}
```

### 3.3 Modbus 采集值 → 虚拟传感器

```yaml
sensors:
  - product_id: "virtual-th"
    device_id: "th-001"
    source_pattern: "s1_*"          # 匹配父设备中 s1_ 开头的所有属性
    property_map:
      s1_temperature: temperature
      s1_humidity: humidity
```

也支持精确 `source_key` 或通配符 `*`、`?`。

### 3.4 模拟传感器

用于在没有真实传感器的情况下演示多设备场景：

```yaml
simulated:
  - product_id: "virtual-pir"
    device_id: "pir-001"
    interval_seconds: 5
    properties:
      - key: occupancy
        type: random_choice
        choices: [0, 1]
        weights: [0.75, 0.25]
```

支持 `random_int`、`random_float`、`random_choice`、`const` 四种类型。

## 4. 运行

```bash
# 1. 进入目录
cd D:\8-relay\virtual-device-bridge

# 2. 安装依赖（首次）
"C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Scripts\pip.exe" install -r requirements.txt

# 3. 启动桥接器
"C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Scripts\python.exe" bridge.py
```

看到类似日志即表示成功：

```text
[2026-09-08T17:30:00.000][INFO] MQTT connected
[2026-09-08T17:30:00.001][INFO] subscribe child cmd: /virtual-lock/lock-001/service/cmd
[2026-09-08T17:30:00.002][INFO] HTTP API 启动: http://0.0.0.0:8080/health
```

## 5. 与 JetLinks 平台对接

1. 在 JetLinks 创建 4 个虚拟产品：
   - `virtual-lock`：属性 `lockState`（枚举或布尔），功能 `setLock`。
   - `virtual-light`：属性 `switch`、`voltage`、`current`、`power`，功能 `turnOn/turnOff/switch`。
   - `virtual-ac`：属性 `powerState`，功能 `setPower`。
   - `virtual-th`：属性 `temperature`、`humidity`。

2. 在每个产品下创建设备：
   - `lock-001`、`light-001`、`light-002`、`ac-001`、`th-001`、`pir-001`、`smoke-001`。

3. 把每个设备的 MQTT 接入认证信息与桥接器使用的 broker 对齐（JetLinks 默认使用 EMQX）。

4. 桥接器启动后，会自动把父网关数据拆到各子设备；在 JetLinks 设备列表即可看到各虚拟设备在线并上报属性。

## 6. 下行命令测试

开灯（灯1，通道 2）：

```bash
mosquitto_pub -h 172.16.4.211 -p 9783 -u test -P 123456 \
  -t "/virtual-light/light-001/service/cmd" \
  -m '{"functionId":"turnOn","inputs":[]}'
```

或写属性：

```bash
mosquitto_pub -h 172.16.4.211 -p 9783 -u test -P 123456 \
  -t "/virtual-light/light-001/service/cmd" \
  -m '{"properties":{"switch":true}}'
```

桥接器会转成 `set_channel` 命令发给父网关，继电器通道 2 吸合。

## 7. HTTP API

- `GET http://localhost:8080/health` —— 连接状态与转发统计
- `GET http://localhost:8080/config` —— 当前加载的映射配置

## 8. 扩展更多传感器

只需在 `config.yaml` 增加 `sensors` 或 `simulated` 条目，重启桥接器即可，不需要改代码：

```yaml
simulated:
  - product_id: "virtual-door"
    device_id: "door-001"
    interval_seconds: 3
    properties:
      - key: doorState
        type: random_choice
        choices: ["open", "closed"]
        weights: [0.2, 0.8]
```

## 9. 部署建议

- 生产环境建议使用 `systemd` 或 `supervisor` 守护进程运行；
- 可运行多个实例（使用不同 `client_id`）做冷备，但注意模拟传感器会重复上报；
- 如需 7×24 稳定运行，建议放到服务器/Docker 容器里，而不是笔记本上。

## 10. 注意事项

- 桥接器不保存状态，所有数据来自 MQTT，重启后从最新消息继续。
- 子设备的 `product_id`/`device_id` 必须和 JetLinks/EMQX 里注册的一致，否则消息会被 broker 拒收。
- 父网关目前使用 `direct` topic 模式（`/{product_id}/{device_id}/property/post`），如改为 `sys` 模式，请同步修改 `child_topic_mode`。
