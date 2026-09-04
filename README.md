# 8 路继电器模拟终端（JetLinks relay8_lfx 完整闭环）

基于 MQTT/JetLinks 的 8 路继电器模拟终端：对接 `relay8_lfx` 产品与 `RELAY8-TERM-01` 设备，实现完整物模型闭环（属性上报 + 下行控制 + 功能回复 + 事件），并经 EMQX 规则透传至平台。

## 项目结构（重构版 v3.0）

```
8-relay/
├── main.py                  # 入口：python main.py [--interval 5]
├── config.py                # 连接配置（MQTT/JetLinks/EMQX/产品设备，请按实际填写）
├── requirements.txt         # 依赖：paho-mqtt>=2.0
├── core/
│   ├── simulator.py         # RelaySimulator：上线/属性上报/事件直发/功能回复
│   └── channel_bank.py      # 8 路通道状态机（开/关 + 电压/电流/功率）
├── setup/                   # 平台初始化脚本（幂等，可重复执行）
│   ├── relay8_thing_model.py    # relay8_lfx 物模型定义（34 属性 + 事件 + 功能）
│   ├── create_product.py        # 创建产品（不存在时）
│   ├── upgrade_product_model.py # 就地升级/校验物模型
│   ├── create_device.py         # 创建设备（RELAY8-TERM-01）
│   └── create_rules.py          # EMQX 3 条透传规则（属性/回复/命令）
└── verify/
    └── e2e_verify.py        # 端到端闭环验证（18 项断言）
```

## 物模型与链路

- 属性（34 项）：`ch1..8` 的 `_state`（bool）、`_voltage`、`_current`、`_power`，另有 `temperature`、`humidity`
- 事件：`switch_change`（属性变化时上报）
- 功能：`set_channel`（单路）、`switch_all`（全开/全关），异步执行，回复 `{messageId, success, output}` 规范报文
- EMQX 规则（3 条，纯透传）：
  1. `rule_lfx_relay8_property`：`/relay8_lfx/+/property/post` → `properties/report`
  2. `rule_lfx_relay8_reply`：`/relay8_lfx/+/function/post` → `function/invoke/reply`
  3. `rule_lfx_relay8_cmd`：`/relay8_lfx/+/function/invoke` → `service/cmd`

## 快速开始

```bash
pip install -r requirements.txt
python main.py                    # 默认每 5s 上报一次，Ctrl+C 退出
python main.py --interval 2       # 自定义上报间隔
```

平台初始化（已执行过可跳过，脚本幂等）：

```bash
python setup/create_product.py         # 1. 建产品（不存在才建）
python setup/upgrade_product_model.py  # 2. 升级/校验物模型
python setup/create_device.py          # 3. 建设备
python setup/create_rules.py           # 4. 建 EMQX 规则
```

端到端验证（需模拟器在线，会真实调用平台接口控制继电器并断言）：

```bash
python verify/e2e_verify.py
```

## 版本历史与回溯

本仓库保留完整提交历史与里程碑标签，可随时回滚：

| 标签 | 说明 |
|------|------|
| `v2.0-jetlinks-full-loop` | 双终端（温湿度/Modbus）JetLinks 全闭环 |
| `v2.1-relay8-lfx` | 8 路继电器接入 relay8_lfx（扁平脚本版） |
| `v2.1.1-no-autoflip` | 去除随机翻转演示 |
| `v2.1.2-no-sim-drift` | 去除 Modbus 从站数值漂移 |
| `v3.0-relay8-restructured` | 重构为 core/setup/verify 分包结构（本版） |

- SourceTree：工具栏"仓库 → 检出新提交/分支/标签"，或在提交图右键 → 检出此提交即可回溯。
- 命令行：`git checkout v3.0-relay8-restructured`（检出后请用新分支继续开发，避免游离 HEAD）。

> 说明：`config.py` 含本组平台账号口令（已按仓库惯例随版本入库），请勿将本仓库设置为公开或外传凭据。
