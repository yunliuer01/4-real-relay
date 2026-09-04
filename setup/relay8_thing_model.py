# -*- coding: utf-8 -*-
"""8 路继电器 relay8_lfx 物模型（与 demo relay_8ch_product 对齐）

结构：34 个属性 + 1 个事件 + 2 个功能
  属性：ch1..ch8 每路 状态(boolean 启动/关闭) + 电压(V) + 电流(A) + 功率(W)，
        环境温度(°C)、环境湿度(%RH)
  事件：switch_change {channel:int, state:boolean}
  功能：set_channel(channel 1-8, state) / switch_all(state)，异步，输出 boolean

被 setup/create_product.py、setup/upgrade_product_model.py 共用。
"""
import json

_BOOL = {"type": "boolean", "trueText": "启动", "trueValue": "true",
         "falseText": "关闭", "falseValue": "false"}


def _float(unit, scale=2):
    return {"type": "float", "unit": unit, "scale": scale}


def _state_expands():
    # 状态属性：设备上报 / 平台直写 / 读，允许平台编辑（同 demo）
    return {"source": "device", "storage": True,
            "type": ["report", "write", "read"],
            "groupId": "group_1", "groupName": "分组_1",
            "storageType": "direct", "otherEdit": True}


def _ro_expands():
    # 只读采集属性：设备上报 / 读
    return {"source": "device", "storage": True,
            "type": ["report", "read"],
            "groupId": "group_1", "groupName": "分组_1"}


def build_metadata():
    props = []
    for i in range(1, 9):
        props.append({"id": f"ch{i}_state", "name": f"通道{i}状态",
                      "expands": _state_expands(), "valueType": dict(_BOOL)})
        props.append({"id": f"ch{i}_voltage", "name": f"通道{i}电压",
                      "expands": _ro_expands(), "valueType": _float("V")})
        props.append({"id": f"ch{i}_current", "name": f"通道{i}电流",
                      "expands": _ro_expands(), "valueType": _float("A")})
        props.append({"id": f"ch{i}_power", "name": f"通道{i}功率",
                      "expands": _ro_expands(), "valueType": _float("W")})
    props.append({"id": "temperature", "name": "环境温度",
                  "expands": _ro_expands(), "valueType": _float("°C")})
    props.append({"id": "humidity", "name": "环境湿度",
                  "expands": _ro_expands(), "valueType": _float("%RH")})

    events = [{
        "id": "switch_change",
        "name": "通道状态变化",
        "valueType": {"type": "object", "properties": [
            {"id": "channel", "name": "通道号", "valueType": {"type": "int"}},
            {"id": "state", "name": "当前状态", "valueType": dict(_BOOL)},
        ]},
        "expands": {"level": "info"},
    }]

    funcs = [
        {"id": "set_channel", "name": "设置单路通道", "expands": {},
         "async": True,
         "inputs": [
             {"expands": {"required": True}, "id": "channel",
              "name": "通道编号", "valueType": {"type": "int", "min": 1, "max": 8}},
             {"expands": {"required": True}, "id": "state",
              "name": "开关状态", "valueType": dict(_BOOL)},
         ],
         "output": {"type": "boolean", "trueText": "执行成功",
                    "trueValue": "true", "falseText": "执行失败",
                    "falseValue": "false"}},
        {"id": "switch_all", "name": "控制所有通道", "expands": {},
         "async": True,
         "inputs": [
             {"expands": {"required": True}, "id": "state",
              "name": "开关", "valueType": dict(_BOOL)},
         ],
         "output": {"type": "boolean", "trueText": "执行成功",
                    "trueValue": "true", "falseText": "执行失败",
                    "falseValue": "false"}},
    ]
    return json.dumps({"properties": props, "events": events,
                       "functions": funcs}, ensure_ascii=False)


def metadata_summary(meta_str):
    md = json.loads(meta_str)
    return (len(md.get("properties", [])), len(md.get("events", [])),
            len(md.get("functions", [])))
