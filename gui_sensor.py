# -*- coding: utf-8 -*-
"""GUI 温湿度模拟器：滑块调整数值，一键/自动上报 MQTT，同时写入 JSON 文件。

用法：
    python gui_sensor.py                    # 默认设备 FILE-TERM-01
    python gui_sensor.py --device MY-TERM   # 自定义设备 ID

说明：
- 界面直接内置 MQTT 上报（无需再开 terminal_file.py）
- 每次上报同时写入 data/sensor_data.json，保持文件与消息一致
- 自动订阅本设备主题，回显服务器转发的消息，验证闭环
"""
import argparse
import json
import logging
import os
import queue
import tkinter as tk
from tkinter import ttk, messagebox

import config
from mqtt_base import MqttReporter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "sensor_data.json")


class SensorGui:
    """温湿度模拟器主界面"""

    def __init__(self, root: tk.Tk, device_id: str,
                 host: str = None, port: int = None):
        self.root = root
        self.device_id = device_id
        self.data_topic = config.data_topic(device_id)
        self.status_topic = config.status_topic(device_id)

        # 初始值：优先读取已有 JSON 文件
        self.temperature = tk.DoubleVar(value=25.5)
        self.humidity = tk.DoubleVar(value=60.0)
        self.auto_report = tk.BooleanVar(value=True)
        self._load_from_file()

        # 跨线程消息队列：MQTT 回调线程 -> Tk 主线程
        self.ui_queue = queue.Queue()

        self.reporter = MqttReporter(device_id, "file",
                                     host=host, port=port)
        self._setup_mqtt()
        self._build_ui()

    # ---------- MQTT ----------
    def _setup_mqtt(self):
        c = self.reporter.client
        c.on_message = self._on_message
        # 在连接成功回调里追加订阅（保留原有 on_connect 逻辑）
        orig_on_connect = c.on_connect

        def on_connect(client, userdata, flags, rc, properties=None):
            orig_on_connect(client, userdata, flags, rc, properties)
            if rc == 0:
                client.subscribe([(self.data_topic, 1),
                                  (self.status_topic, 1)])
        c.on_connect = on_connect
        self.reporter.start()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
        self.ui_queue.put(("msg", msg.topic, payload))

    # ---------- UI ----------
    def _build_ui(self):
        self.root.title(f"温湿度模拟器 - {self.device_id}")
        self.root.geometry("520x560")
        self.root.minsize(480, 520)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # 顶部状态栏
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.conn_label = ttk.Label(top, text="● 连接中...",
                                    foreground="#e6a700", font=("微软雅黑", 10, "bold"))
        self.conn_label.pack(side="left")
        ttk.Label(top, text=f"{config.MQTT_HOST}:{config.MQTT_PORT}",
                  foreground="#888").pack(side="right")

        # 温湿度控制区
        panel = ttk.LabelFrame(self.root, text=" 传感器数值 ", padding=(15, 10))
        panel.pack(fill="x", padx=10, pady=6)

        # 温度行
        row1 = ttk.Frame(panel)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="温度 ℃", width=8).pack(side="left")
        self.temp_scale = ttk.Scale(row1, from_=-10, to=50, variable=self.temperature,
                                    command=self._on_scale_drag)
        self.temp_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.temp_spin = ttk.Spinbox(row1, from_=-10, to=50, increment=0.1, width=6,
                                     textvariable=self.temperature,
                                     command=self._on_value_change)
        self.temp_spin.pack(side="left")

        # 湿度行
        row2 = ttk.Frame(panel)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="湿度 %RH", width=8).pack(side="left")
        self.hum_scale = ttk.Scale(row2, from_=0, to=100, variable=self.humidity,
                                   command=self._on_scale_drag)
        self.hum_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.hum_spin = ttk.Spinbox(row2, from_=0, to=100, increment=0.1, width=6,
                                    textvariable=self.humidity,
                                    command=self._on_value_change)
        self.hum_spin.pack(side="left")

        # 滑块松开 = 结束调整（自动上报触发点）
        for scale in (self.temp_scale, self.hum_scale):
            scale.bind("<ButtonRelease-1>", self._on_scale_release)
        # 数值框回车确认
        for spin in (self.temp_spin, self.hum_spin):
            spin.bind("<Return>", lambda e: self._on_value_change())

        # 按钮区
        btns = ttk.Frame(self.root, padding=(10, 4))
        btns.pack(fill="x")
        self.report_btn = ttk.Button(btns, text="立即上报", command=self.do_report)
        self.report_btn.pack(side="left", padx=(0, 8))
        ttk.Checkbutton(btns, text="拖动滑块后自动上报",
                        variable=self.auto_report).pack(side="left")

        # 日志区
        log_frame = ttk.LabelFrame(self.root, text=" 消息日志（含服务器回传） ", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_text = tk.Text(log_frame, height=14, state="disabled",
                                font=("Consolas", 9), wrap="word",
                                background="#fafafa")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log_text.tag_configure("error", foreground="#d93025")

        # 关闭清理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 轮询队列刷新 UI
        self._poll_queue()
        self._poll_connection()

    # ---------- 事件 ----------
    def _on_scale_drag(self, _=None):
        """拖动过程中只刷新数值显示，不上报"""
        self.root.title(f"温湿度模拟器 - {self.device_id}")

    def _on_scale_release(self, _=None):
        self._sync_spinboxes()
        if self.auto_report.get():
            self.do_report()

    def _on_value_change(self):
        self._sync_spinboxes()
        if self.auto_report.get():
            self.do_report()

    def _sync_spinboxes(self):
        try:
            self.temp_spin.configure(textvariable=self.temperature)
            self.hum_spin.configure(textvariable=self.humidity)
        except Exception:
            pass

    # ---------- 上报 ----------
    def do_report(self):
        try:
            temp = round(float(self.temperature.get()), 2)
            hum = round(float(self.humidity.get()), 2)
        except (tk.TclError, ValueError):
            messagebox.showwarning("数值错误", "温湿度数值无效，请检查输入")
            return

        # 1) 写入 JSON 文件（保持与文件终端一致）
        try:
            os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"temperature": temp, "humidity": hum}, f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            self._log(f"写入文件失败: {e}", tag="error")

        # 2) 直接 MQTT 上报
        ok = self.reporter.report(temp, hum)
        if ok:
            self._log(f"已上报：温度={temp}℃ 湿度={hum}%RH -> {self.data_topic}")
        else:
            self._log("上报失败，请检查 MQTT 连接", tag="error")

    # ---------- UI 刷新 ----------
    def _log(self, text, tag=None):
        self.log_text.configure(state="normal")
        if tag == "error":
            self.log_text.insert("end", text + "\n", "error")
        else:
            self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self):
        """把 MQTT 线程收到的消息转移到主线程显示"""
        try:
            while True:
                kind, topic, payload = self.ui_queue.get_nowait()
                self._log(f"[收到] {topic}: {payload}", tag=None)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _poll_connection(self):
        if self.reporter.connected:
            self.conn_label.configure(text="● 已连接", foreground="#1a9c48")
        else:
            self.conn_label.configure(text="● 连接中...", foreground="#e6a700")
        self.root.after(1000, self._poll_connection)

    def _load_from_file(self):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.temperature.set(float(data["temperature"]))
            self.humidity.set(float(data["humidity"]))
        except Exception:
            pass  # 文件不存在或损坏则用默认值

    def _on_close(self):
        self.reporter.stop()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="GUI 温湿度模拟器")
    parser.add_argument("--device", default="FILE-TERM-01", help="设备 ID")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 端口")
    args = parser.parse_args()

    root = tk.Tk()
    gui = SensorGui(root, args.device, host=args.host, port=args.port)

    gui._log(f"设备 ID：{args.device}")
    gui._log(f"上报主题：{gui.data_topic}")
    gui._log("提示：拖动滑块松手即上报（可关闭自动上报改用手动按钮）")
    root.mainloop()


if __name__ == "__main__":
    main()
