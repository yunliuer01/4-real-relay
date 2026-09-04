# 固件 PC 仿真测试台（无硬件）

在没有 ESP32 硬件的情况下，用 Python 桩(stub)在 PC 上完整跑
`esp32-8relay-firmware/main.py` 固件逻辑，并且**真实连接内网 EMQX / JetLinks**，
验证整条业务链路。

当前实测结果：**18 / 18 全过**（A~F 六个阶段）。

## 运行要求

- Windows / Linux / macOS 均可，需 Python 3.9+（开发环境用 Python 3.14 验证）
- `pip install paho-mqtt`
- 完整跑 C~E 需能访问项目配置的 MQTT broker（见仓库根目录 `config.py` 的
  `MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASS`）

## 快速开始

```powershell
# 只验证 配网/保存重启/持久化/长按回配网（不依赖 broker，任何机器可跑）
python firmware-simtest\run_sim.py --no-mqtt

# 全链路：配网 -> 连真实 EMQX -> 属性上报/下行命令/断线重连/长按回配网
python firmware-simtest\run_sim.py
```

运行结束后若需人工再确认配网页，浏览器打开 <http://127.0.0.1:18080>。

## 测试流程（A~F）

| 阶段 | 内容 | 验证点 |
| --- | --- | --- |
| A | 全新设备首启（flash 无 config.json）→ 进入 AP 配网模式 | `GET /` 返回配置页 |
| B | 通过配网页 `POST /save` 提交配置 → 固件 `machine.reset()` 重启 | 返回 200、boot>=2、config.json 已持久化 |
| C | 重启后读配置 → 连 WiFi(桩) → 连**真实 EMQX** | 属性上报 topic 收到，properties 含 8 路状态+温湿度，payload 含 productId/deviceId/timestamp |
| D | 平台下行命令 | properties/write、service/cmd(set_channel、switch_all) 均回复 success；GPIO 电平变化正确；上报 switch_change 事件 |
| E | 模拟网络断开/恢复 | 固件自动感知断线、自动重连、重连后重新上报属性 |
| F | 长按 SW1(5s) | 退出正常模式，重新进入配网模式，`GET /` 再次可访问 |

结果用 `PASS n/n` 汇总，任一 FAIL 进程退出码为 1。

## 目录结构

```
firmware-simtest/       # 位于仓库根目录，与 esp32-8relay-firmware/ 平级
├── run_sim.py          # 主测试脚本（测试驱动 + 固件运行容器 + MQTT 观察端）
├── flash/              # 运行生成的“虚拟 flash”，已 gitignore
└── stubs/              # MicroPython 桩，模拟真机 API
    ├── machine.py      #   Pin/电平模拟、machine.reset() -> SimReset
    ├── network.py      #   WLAN STA/AP，可控制连接成功/断开
    ├── time.py         #   ticks_ms/ticks_add/ticks_diff/sleep_ms 等补丁
    ├── socket.py       #   全量透传真实 socket，仅强制 send 收 bytes + 端口重映射
    └── umqtt/simple.py #   umqtt.simple 桥接 paho-mqtt，bytes 语义与真机一致
```

### 桩与真机的一致性（为什么要这么写）

- **MicroPython umqtt.simple 要求 bytes**：topic/payload/client_id 传 str 会抛
  TypeError。桩 `stubs/umqtt/simple.py` 也强制 bytes，因此**固件里任何漏编码的
  地方在 PC 仿真时会直接暴露**（这些问题上真机同样会跑挂）。
- **低电平吸合**：`Pin.value(0)`=导通/按下，`Pin.value(1)`=断开/松开，与继电器
  硬件语义一致；测试通过 `machine.sim_read(pin)` 断言 GPIO 电平。
- **重启语义**：`machine.reset()` 抛 `SimReset`，由 `run_sim.py` 捕获后以全新
  命名空间重新 exec 固件，模拟掉电重启（flash 状态保留在 `firmware-simtest/flash/`）。
- **网络断开**：`umqtt.simple` 桩的 `sim_down()/sim_up()` 模拟链路中断；`network`
  桩的 `sim_set_sta_ok()` 可模拟连不上 WiFi。

## 关键参数（命令行）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--no-mqtt` | 关 | 跳过 C~E（不连真实 broker） |
| `--device-id` | SIM-DEV-01 | 仿真设备 ID，注意别与平台上正在运行的同一 ID 设备顶号 |
| `--web-port` | 18080 | 固件 80 端口被映射到的本机端口 |
| `--mqtt-host/port/user/pass` | 读根目录 config.py | 手动指定 broker |

## 注意事项

- 仿真用的设备 ID 会真实出现在 EMQX/JetLinks 上。跑测试前请确认该 ID 未被占用，
  否则会出现“顶号”相互踢下线。
- `run_sim.py` 每次运行会删除 `firmware-simtest/flash/config.json`（模拟全新设备首启）。
- 断线重连依赖 broker 保活/会话语义，E 阶段在 EMQX 5.x 上验证通过。
