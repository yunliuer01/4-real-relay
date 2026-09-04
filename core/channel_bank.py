# -*- coding: utf-8 -*-
"""8 路继电器状态与电参模拟（RelayChannelBank，True=开 / False=关）

对齐 JetLinks 产品物模型（8 路 * 状态/电压/电流/功率 + 环境温度/湿度）：
  - 通道开启：电压≈市电 220V 上下微抖、电流为该路负载大小、功率=电压×电流
  - 通道关闭：电压/电流/功率均为 0
  - 环境温度/湿度做随机游走，维持小区间波动
"""
import random
import time


def _to_bool(v):
    """宽松布尔转换：True/1/'1'/'true'/'on' -> True，其余 -> False"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _round(v, nd=2):
    return round(v, nd)


class RelayChannelBank:
    """8 路继电器状态 + 电参 + 温湿度模拟"""

    def __init__(self, channel_count=8):
        self.channel_count = channel_count
        self.states = [False] * channel_count
        self.voltage = [0.0] * channel_count   # V
        self.current = [0.0] * channel_count   # A
        self.power = [0.0] * channel_count     # W
        # 每路负载特征：电压基准 + 电流基准(重载通道电流更大)
        self._v_base = [round(random.uniform(216.0, 224.0), 1)
                        for _ in range(channel_count)]
        self._i_base = [round(random.uniform(0.02, 0.4), 2) for _ in range(channel_count)]
        # 随机挑两路作为重载（大电流）负载
        for idx in random.sample(range(channel_count), 2):
            self._i_base[idx] = round(random.uniform(4.0, 9.5), 2)
        # 环境
        self.temperature = round(random.uniform(26.0, 28.0), 2)
        self.humidity = round(random.uniform(45.0, 55.0), 2)

    # ---------- 控制 ----------
    def set_channel(self, index1, on):
        """设置单个通道（1 起）；返回 True 表示状态发生变化"""
        if not (1 <= index1 <= self.channel_count):
            return False
        on = _to_bool(on)
        idx = index1 - 1
        if self.states[idx] == on:
            return False
        self.states[idx] = on
        if on:  # 首次上电给一个初始电参
            v = round(self._v_base[idx] * random.uniform(0.99, 1.01), 2)
            i = round(self._i_base[idx] * random.uniform(0.95, 1.05), 2)
            self.voltage[idx] = v
            self.current[idx] = i
            self.power[idx] = _round(v * i)
        else:
            self.voltage[idx] = 0.0
            self.current[idx] = 0.0
            self.power[idx] = 0.0
        return True

    def set_all_state(self, on):
        """所有通道统一开关；返回实际改变的通道列表"""
        on = _to_bool(on)
        changed = [i for i in range(1, self.channel_count + 1)
                   if self.states[i - 1] != on]
        for i in range(1, self.channel_count + 1):
            self.set_channel(i, on)
        return changed

    def set_all(self, **kw):
        """按 {ch1_state: bool} 或 {r1: 1} 键写多个通道；返回改变的通道列表"""
        changed = []
        for key, val in kw.items():
            m = None
            if key.startswith("ch") and key.endswith("_state"):
                try:
                    m = int(key[2:-6])  # ch1_state -> 1
                except ValueError:
                    continue
            elif key.startswith("r") and key[1:].isdigit():
                m = int(key[1:])
            if m and 1 <= m <= self.channel_count:
                if self.set_channel(m, val):
                    changed.append(m)
        return changed

    # ---------- 采集 ----------
    def tick(self):
        """每次上报前微调温湿度与开启通道的电参"""
        self.temperature += random.uniform(-0.05, 0.05)
        self.humidity += random.uniform(-0.2, 0.2)
        self.temperature = min(35.0, max(20.0, self.temperature))
        self.humidity = min(70.0, max(30.0, self.humidity))
        self.temperature = _round(self.temperature)
        self.humidity = _round(self.humidity)
        for i in range(self.channel_count):
            if not self.states[i]:
                continue
            v = self._v_base[i] * random.uniform(0.995, 1.005)
            i_cur = self._i_base[i] * random.uniform(0.98, 1.02)
            self.voltage[i] = _round(v)
            self.current[i] = _round(i_cur)
            self.power[i] = _round(v * i_cur)

    def snapshot(self):
        """生成上报用属性字典（34 个属性键）"""
        props = {}
        for i in range(1, self.channel_count + 1):
            s = self.states[i - 1]
            props[f"ch{i}_state"] = s
            props[f"ch{i}_voltage"] = round(self.voltage[i - 1], 2) if s else 0.0
            props[f"ch{i}_current"] = round(self.current[i - 1], 2) if s else 0.0
            props[f"ch{i}_power"] = round(self.power[i - 1], 2) if s else 0.0
        props["temperature"] = self.temperature
        props["humidity"] = self.humidity
        return props

    @staticmethod
    def state_desc(states):
        return " ".join(f"{i}={1 if s else 0}"
                        for i, s in enumerate(states, 1))


# 兼容旧接口（to_report）
if __name__ == "__main__":
    import time  # noqa: F401
    bank = RelayChannelBank()
    bank.set_all_state(True)
    bank.tick()
    snap = bank.snapshot()
    print(len(snap), sorted(snap.items())[:6])
