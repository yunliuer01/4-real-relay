# 固件 PC 仿真测试台（无硬件）

在没有 ESP32 硬件的情况下，用 Python 桩(stub)在 PC 上完整跑固件逻辑，
并连接 **真实 EMQX / JetLinks** 验证整条业务链路；即使不在内网（broker 不可达），
`run_sim_modbus.py` 会自动切换“环回录播”后端，本地完成收发验证。

当前有两个测试台，分别覆盖两版固件：

| 测试台 | 被测固件 | 覆盖内容 | 实测 |
| --- | --- | --- | --- |
| `run_sim.py` | `esp32-8relay-firmware/main.py`（4 路，无 Modbus） | 配网/持久化/继电器 MQTT 控制/断线重连/长按回配网 | 18 / 18 |
| `run_sim_modbus.py` | `esp32-relay4-modbus-gateway/main.py` + `modbus_master.py` | 上述全部 + Modbus 采集网关（多从站/多周期/不阻塞/故障恢复） | 24 / 24 |

## 运行要求

- Windows / Linux / macOS 均可，需 Python 3.9+
- `pip install paho-mqtt`
- 跑 `run_sim.py` 完整 C~E 需能访问 broker（见仓库根目录 `config.py` 的
  `MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASS`）；`run_sim_modbus.py` 无需 broker
  （broker 可达时自动走真实链路，否则走本地环回）

## 快速开始

```powershell
# 继电器固件（无 Modbus）：只验 配网/持久化/长按（任何机器可跑）
python firmware-simtest\run_sim.py --no-mqtt

# 继电器固件：全链路（需能访问真实 EMQX）
python firmware-simtest\run_sim.py

# Modbus 网关固件：任意机器可跑（broker 可达则真连，否则自动环回）
python firmware-simtest\run_sim_modbus.py

# 强制走真实 broker / 强制环回
python firmware-simtest\run_sim_modbus.py --backend real
python firmware-simtest\run_sim_modbus.py --backend loopback
```

浏览器人工确认配网页（仅测试运行中可打开）：`run_sim.py` → <http://127.0.0.1:18080>；
`run_sim_modbus.py` → <http://127.0.0.1:18081>。

## run_sim.py 测试流程（A~F，4 路继电器，无 Modbus）

| 阶段 | 内容 | 验证点 |
| --- | --- | --- |
| A | 全新设备首启（flash 无 config.json）→ AP 配网模式 | `GET /` 返回 4 路配置页 |
| B | `POST /save` → `machine.reset()` 重启 | 200、boot>=2、config.json 持久化 |
| C | 连 WiFi(桩) → 连真实 EMQX | 属性上报含 4 路状态+温湿度、payload 结构 |
| D | 平台下行命令 | properties/write、set_channel、switch_all 回复 success、GPIO 正确、switch_change 事件 |
| E | 模拟网络断开/恢复 | 固件自动重连并重新上报属性 |
| F | 长按 SW1(IO10, 5s) | 回到配网模式，页面可访问 |

## run_sim_modbus.py 测试流程（A~G，Modbus 网关固件）

关键验证目标：**采集线程不阻塞继电器**、采集值合并上报、关键日志、故障恢复。

| 阶段 | 内容 | 验证点 |
| --- | --- | --- |
| A | 首启 → 配网页 | 页面为 4 路版且含 `modbus_json` 配置框（预填 JSON） |
| B | `POST /save`（带 2 从站 Modbus JSON）→ 重启 | 200、boot>=2、config.json 持久化 Modbus 配置 |
| C | 正常模式采集 | 属性含 4 路状态 + `s1_temperature`/`s1_humidity`/`s2_mb_value`（换算正确：raw250·scale0.1→25.0）；`[MODBUS]` 关键日志（从站/地址/原始值/换算值）；改寄存器后下次上报生效 |
| D | 从站 1 假死（超时重试中） | 出现 `attempt=` 重试日志；期间下发 set_channel **<3s 秒回**、GPIO 正确、事件上报；恢复后采集日志再现 |
| E | 从站 2 掉线 / 恢复 | `[MODBUS] ERROR` 日志；故障期间上报保留最近一次值（不丢）；恢复并改寄存器后新值生效 |
| G | MQTT 断线（模拟） | 断线 4s 内 Modbus 采集日志持续输出（线程独立）；恢复后自动重连并重新上报 |
| F | 长按 SW1(IO10, 5s) | 回到配网模式；`Modbus worker thread stopped`（线程优雅停止） |

### 仿真 Modbus 从站能力（machine 桩内置）

- 多从站多寄存器表，`sim_modbus_add_slave / sim_modbus_set / sim_modbus_get`
- 故障注入：`sim_modbus_down`（无应答→主站超时）、`sim_modbus_exc`（异常码）、
  `sim_modbus_crc_bad`（应答 CRC 故意写错）
- 与真实 RTU 一致：从站不存在/请求 CRC 错 → 不回帧；地址越界 → 异常码 0x02

## 目录结构

```
firmware-simtest/
├── run_sim.py            # 继电器固件(4路无Modbus)测试台
├── run_sim_modbus.py     # Modbus 网关固件测试台（环回/真实双后端）
├── flash/                # run_sim.py 的虚拟 flash（已 gitignore）
├── flash_modbus/         # run_sim_modbus.py 的虚拟 flash（已 gitignore）
└── stubs/                # MicroPython 桩
    ├── machine.py        #   Pin/电平、machine.reset()、UART + 仿真 Modbus 从站总线
    ├── network.py        #   WLAN STA/AP
    ├── time.py           #   ticks_ms/ticks_add/ticks_diff/sleep_ms 等补丁
    ├── socket.py         #   透传真实 socket，80 端口重映射
    └── umqtt/simple.py   #   umqtt.simple 桥接 paho-mqtt + 环回录播模式
```

### 桩与真机的一致性（为什么要这么写）

- **umqtt.simple 要求 bytes**：topic/payload 传 str 抛 TypeError，与真机一致——
  固件漏编码在 PC 仿真直接暴露。
- **低电平吸合**：`Pin.value(0)`=导通/按下；测试用 `machine.sim_read(pin)` 断言。
- **重启语义**：`machine.reset()` 抛 `SimReset`，测试台捕获后以全新命名空间重新
  exec 固件，模拟掉电重启（flash 状态保留）。
- **Modbus RTU**：UART 桩把固件写入的请求帧直接交给内存从站总线，按 RTU 协议回
  应答；`_thread` 用 CPython 真实线程，Modbus 采集与固件主循环真并发——因此
  “不阻塞继电器”的验证是真实有效的。
- **环回录播**：broker 不可达时 `sim_set_loopback(True)` 让固件在本地“在线”，
  上行消息可断言、下行消息可注入，任何机器可跑全链路。

## 关键参数（命令行）

| 测试台 | 参数 | 默认 | 说明 |
| --- | --- | --- | --- |
| run_sim | `--no-mqtt` | 关 | 跳过 C~E |
| run_sim | `--device-id` | SIM-DEV-01 | 仿真设备 ID |
| run_sim | `--web-port` | 18080 | 配网页映射端口 |
| run_sim_modbus | `--backend` | auto | auto=broker 可达则 real，否则 loopback；可强制 real/loopback |
| run_sim_modbus | `--web-port` | 18081 | 配网页映射端口 |

## 注意事项

- 仿真设备 ID 会真实出现在 EMQX/JetLinks 上（real 模式），请确认未占用，否则顶号。
- **real 模式下固件的 `mqtt_user` 必须填平台接入账号（本项目为 `test/123456`），
  不能拿 device_id 当用户名**——EMQX 会对错误账号回 CONNACK rc≠0 拒绝；而 paho 的
  认证失败是异步回调、同步 `connect()` 不抛异常，固件桩曾因此“假在线”，
  broker 消息级断言全部超时（真实模式一度只有 14/24）。现桩已检测 CONNACK rc，
  认证被拒会抛 `OSError("broker refused connection (CONNACK rc=...)")` 并让固件走重连。
- 真实模式回归出现「日志级断言全 PASS、消息级断言全超时」时，优先怀疑 MQTT 账号/ACL，
  用 `paho` 直连探测两个候选账号的 CONNACK rc 即可定位。
- 两个测试台每次运行都会删除各自的 `flash*/config.json`（模拟全新设备首启）。
- `run_sim_modbus.py` 被测固件路径：`esp32-relay4-modbus-gateway/main.py`，
  运行时会把该目录加入 `sys.path` 以便固件 `import modbus_master`。
