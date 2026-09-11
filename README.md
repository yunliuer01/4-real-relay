# 8-relay — ESP32-C3 4 路继电器 + Modbus 采集网关

把一块 **ESP32-C3** 做成 4 路继电器 + Modbus RTU/TCP 采集网关，按 JetLinks 协议接入
**EMQX → JetLinks**，再由一个可编程桥接器把「父设备的一包上行数据」拆解给多个不同产品的
虚拟子设备（门锁、灯、空调、温湿度传感器……），并支持子设备下行命令聚合回网关。

当前主线版本：**v6.0.5**（2026-09-10）。

---

## 仓库总览

```
8-relay/
├── esp32-relay4-modbus-gateway/   ★ 主线固件（MicroPython，v6.0.5）
├── firmware-simtest/                无硬件 PC 仿真测试台（桩 + 真实 broker 双后端）
├── virtual-device-bridge/           虚拟设备映射桥接器（父设备 → 多子设备拆分）
├── setup/                           JetLinks 平台初始化（产品/物模型/设备/规则，幂等）
├── verify/                          平台侧端到端验证（v3.0 遗存）
├── esp32-8relay-firmware/           上一代 4 路固件（无 Modbus），保留作对照
├── main.py  core/  config.py        v3.0 的 8 路继电器模拟终端（纯 Python，历史模块）
├── reference/                       平台设备 JSON 参考快照
└── archive/                         会话调试产物归档（不入库，见目录内 README）
```

---

## 主线：`esp32-relay4-modbus-gateway/`

### 固件构成

| 文件 | 作用 |
|---|---|
| `main.py` | 主程序源码（约 96 KB）：配网、WiFi、MQTT、继电器控制、HTTP API、Modbus 调度、自愈 |
| `main_entry.py` | 板上 `/main.py` 的引导壳（1 KB），负责加载 `.mpy` |
| `app_main.mpy` | `main.py` 经 `mpy-cross` 预编译的产物，**板上实际运行的就是它** |
| `build_mpy.py` | 打包脚本：源码 → `.mpy` |
| `deploy.py` | **一键部署**：内部自动调用构建，再上传到板子（含产物新鲜度守卫） |
| `boot.py` / `wifi_boot.py` | 启动阶段空堆预连 WiFi（规避 esp-sha 内存饥饿） |
| `modbus_master.py` | Modbus **RTU** 主站（UART1：IO20/IO21，DIR=IO8） |
| `modbus_tcp_master.py` | Modbus **TCP** 主站（多从站，独立线程，不阻塞继电器） |
| `portal_page.html` | AP 配网页面（部署到板上为 `portal.html`，另带 `portal_preview.html` 本地预览） |
| `verify_board.py` | **实板验收脚本**：12 项断言，专治「HTTP 并发 ≥4 整机冻结」 |

### ⚠️ 改完代码必须走 `deploy.py`

`.mpy` 是预编译字节码，**只上传源码板子不会重新编译**，会继续跑旧逻辑而且日志看起来
一切正常。`deploy.py` 内置了「产物 mtime 早于源码就硬失败」的守卫，别绕过它直接传文件。

```bash
python esp32-relay4-modbus-gateway/deploy.py            # 构建 + 上传（推荐）
python esp32-relay4-modbus-gateway/deploy.py --skip-build   # 明确表示我就要跳过构建
```

### 功能清单

- **AP 配网**：长按 SW1（IO10）5 秒进入热点 `Relay4-Setuplfx`（`192.168.4.1`），填写参数
  保存到板载 `/config.json`，掉电不丢失，重启自动联网。
- **4 路继电器**：`set_channel`（单路）/ `switch_all`（全路），状态变化上报 `switch_change` 事件。
  继电器 RELAY1~4 = IO3/4/5/7，低电平吸合；LED = IO2。
- **Modbus 采集网关**：RTU 与 TCP 双模式；多从站、每从站多寄存器、各自独立采集周期；
  支持 `scale` / `signed` / `digits` 转换；采集结果自动并入属性上报。
- **HTTP 控制 API**：`/api/relay`、`/api/info` 等，跑在**主循环非阻塞轮询**里
  （不是线程——C3 只给线程 4 KB 栈，早期版本整个板子会被它冻死）。
- **三级自愈**：MQTT 停滞 → 主动重连；WiFi 数据面假死（`isconnected()` 恒 True 但收发包全丢）
  → 主动重新关联；网络彻底不可用 → `machine.reset()` 回 boot 阶段重来。
- **双 Topic 模式**：`direct`（默认，EMQX 规则路径 `/{productId}/{deviceId}/...`）
  与 `sys`（JetLinks MQTT 网关规范路径）。

### 实板验收

```bash
python esp32-relay4-modbus-gateway/verify_board.py            # 12 项断言
python esp32-relay4-modbus-gateway/verify_board.py --serial   # 附带串口日志落盘
```

判据核心：并发阶梯 `1/2/4/8/16/32` 全部 200 **且每一级之后板子立刻还活着**，
并发洪泛期间父设备属性上报不断流，下行 `set_channel` 真实生效。

> 排障铁律：`timeout` = 整机冻结；**立刻/延迟收到 RST 则是 lwIP PCB 池（C3 只有 16 条
> active TCP）的容量拒绝**，属于正常保护，别当成回归。

---

## 配套模块

### `firmware-simtest/` — 无硬件仿真测试台

用 Python 桩模拟 `machine` / `network` / `socket` / `umqtt`，在 PC 上完整跑固件逻辑。
broker 可达就真连，不可达自动切「环回录播」后端，**离线也能验**。

```bash
python firmware-simtest/run_sim.py --no-mqtt        # 4 路固件基础流程
python firmware-simtest/run_sim_modbus.py           # Modbus 网关版
```

v6.0.5 相关回归套件（`test_v604_heartbeat` / `test_v605_http_poll` /
`test_v605_mpy_packaging` / `test_v605_mqtt_preflight` / `test_v605_wifi_selfheal`）
合计 **72/72 PASS**；早期 `run_sim*.py` 为 18/18 与 24/24。

### `virtual-device-bridge/` — 虚拟设备映射桥接器

订阅父网关所有上行消息，按 `config.yaml` 的映射规则拆成多个子设备属性/事件，发布到对应
子设备 topic；同时监听各子设备下行命令，聚合回父网关控制帧。功能等价于 EMQX 规则引擎，
但用 Python 实现，规则可随意扩展。

```bash
pip install -r virtual-device-bridge/requirements.txt
python virtual-device-bridge/bridge.py
```

### `setup/` — JetLinks 平台初始化（幂等，可重复执行）

```bash
python setup/create_product.py         # 建产品
python setup/upgrade_product_model.py  # 升级/校验物模型
python setup/create_device.py          # 创建设备
python setup/create_rules.py           # 建 EMQX 透传规则
```

---

## 环境坐标

| 项 | 值 |
|---|---|
| 板子 | ESP32-C3，device_id `7ce8b1c1a7fc`，IP `192.168.30.160` |
| 板载 product_id（=上行 topic 首段） | `relay4_lfx` |
| MQTT Broker | `172.16.4.211:9783`，账号 `test / 123456` |
| JetLinks | `172.16.4.211:9000`，工作账号 `admin5` |
| WiFi | `Office-WiFi` |
| 运行环境 | `C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（含 pyserial / esptool / paho）<br>系统 `python` 无 pyserial，别用它跑部署脚本 |

> 仓库内配置文件含本组平台与 WiFi 口令，**请勿公开本仓库或外传凭据**。

---

## 版本里程碑

| 标签 | 说明 |
|---|---|
| **`v6.0.5-http-poll-freeze-fix`** | **当前主线**。HTTP API 改主循环非阻塞轮询，修「并发 ≥4 整机冻结」；WiFi 假死自愈；MQTT 建连前带超时 TCP 预检；引入 `.mpy` 预编译打包链路，摆脱源码直编的体积/堆约束 |
| `v6.0.4-mqtt-puback-deadlock-fix` | umqtt `publish(qos=1)` 嵌套 PUBACK 死锁 → 出站全改 `PUB_QOS=0` |
| `v6.0.3-downlink-commandmap-fix` | 补全桥接器 `command_map`，修 JetLinks 下行全链路 |
| `v6.0.2-bridge-timestamp-fix` | 桥接器强制本机时间戳；4 个继电器子设备换新 deviceId |
| `v6.0.1-boot-wifi-preconnect` | 启动阶段空堆预连 WiFi，修 esp-sha 永久饥饿 |
| `v6.0-modbus-tcp-gateway` | 新增 Modbus TCP 主站模式 + 配网页可视化编辑 |
| `v5.4.1-real-board-e2e` | 实板端到端打通（bridge `gateway.product_id=relay4_lfx`） |
| `v5.4-jetlinks-lfx-e2e` | JetLinks 标准 topic + `gateway_topic_mode=direct` |
| `v5.3-virtual-device-bridge` | 引入虚拟设备桥接器 |
| `v5.0-final-complete` | 全部需求完成里程碑（**回滚锚点**） |
| `v4.0-real-relay4-lfx` | 首次实板接入 relay4_lfx |

完整 26 个标签见 `git tag`。

---

## 远端与回滚

| 远端 | 地址 | 说明 |
|---|---|---|
| `realrelay` | `ssh://git@ssh.github.com:443/yunliuer01/4-real-relay.git` | GitHub，**必须走 443 端口**（22 不稳） |
| `gitee-relay` | `git@gitee.com:lfx-229492/4-real-relay.git` | Gitee，SSH 免密可用 |
| ~~`origin` / `gitee`~~ | HTTPS 旧地址 | 已失效/落后，勿用 |

```bash
git checkout v5.0-final-complete      # 回滚到里程碑（检出后请另开分支，避免游离 HEAD）
git push realrelay main --tags        # 推送（沙箱内 refs 被隔离，用显式写法）
```

回滚指引：
- 物理按键异常重启 → 回滚 `v5.0-final-complete`，并把 `config.json` 的 `sw1_pin` / `sw_pin` 置 `null`。
- HTTP 冻结复发 → 先分清**失败签名**（`timeout` 才是冻结，`RST` 是容量拒绝），
  再用实板 `http_polls` / `http_aborts` 计数 + `verify_board.py` 的并发阶梯定位。
- 「改了代码板上没反应」→ 先查 `app_main.mpy` 的 mtime 是否比 `main.py` 新。

---

## 历史模块

- **`main.py` + `core/` + `verify/`**（v3.0）：8 路继电器**纯 Python 模拟终端**，
  对接 `relay8_lfx` 产品做物模型闭环（34 属性 + 事件 + 功能），带 EMQX 透传规则。
  ```bash
  python main.py --interval 5      # 每 5s 上报
  python verify/e2e_verify.py      # 18 项断言
  ```
- **`esp32-8relay-firmware/`**：上一代 4 路继电器固件（无 Modbus）。
  目录名沿用了早期 "8relay" 的叫法，实际内容是 4 路版，作为 v4.x 之前的对照保留。

---

## 目录整理说明

2026-09-11 做过一次整理：仓库根目录此前堆积了 338 个文件（其中 326 个是一次性调试产物），
现已归档到 `archive/session-debug-2026-09/`，根目录只保留 5 个文件。

`archive/` **不进版本库**，只在本机保留可追溯性，详见 `archive/README.md`。
其中 `_verify_v605.py` 已「转正」为受版本管理的
`esp32-relay4-modbus-gateway/verify_board.py`。
