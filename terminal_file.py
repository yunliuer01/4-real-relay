# -*- coding: utf-8 -*-
"""文件监听终端：监听本地温湿度数据文件的变化，手动修改数值后立即上报 MQTT

用法：
    python terminal_file.py                        # 使用默认参数
    python terminal_file.py --file data/sensor_data.json --device-id FILE-TERM-01
"""
import argparse
import json
import logging
import os
import sys
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
from mqtt_base import MqttReporter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("file-terminal")


class SensorFileHandler(FileSystemEventHandler):
    """监听数据文件所在目录，文件被修改时触发上报"""

    def __init__(self, reporter: MqttReporter, file_path: str, deadband: float):
        super().__init__()
        self.reporter = reporter
        self.file_path = os.path.abspath(file_path)
        self.deadband = deadband
        self.last_values = (None, None)
        self._last_trigger = 0.0

    # ---------- 读取与上报 ----------
    def read_and_report(self, force: bool = False):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            temperature = float(data["temperature"])
            humidity = float(data["humidity"])
        except FileNotFoundError:
            log.error("数据文件不存在: %s", self.file_path)
            return
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("数据文件格式有误（需要 JSON 且含 temperature/humidity 字段）: %s", e)
            return

        last_t, last_h = self.last_values
        changed = (last_t is None
                   or abs(temperature - last_t) >= self.deadband
                   or abs(humidity - last_h) >= self.deadband)
        if changed or force:
            if self.reporter.report(temperature, humidity):
                self.last_values = (temperature, humidity)
        else:
            log.info("数值未变化（%.2f℃ / %.2f%%RH），不上报", temperature, humidity)

    # ---------- watchdog 回调 ----------
    def on_modified(self, event):
        if event.is_directory:
            return
        if os.path.abspath(event.src_path) != self.file_path:
            return
        now = time.time()
        # 防抖：编辑器保存常触发多次事件，300ms 内只处理一次
        if now - self._last_trigger < 0.3:
            return
        self._last_trigger = now
        log.info("检测到文件变化: %s", event.src_path)
        time.sleep(0.1)  # 等文件写入完成，避免读到半截内容
        self.read_and_report()


def main():
    parser = argparse.ArgumentParser(description="文件监听终端：监听本地文件温湿度变化并上报 MQTT")
    parser.add_argument("--file", default=os.path.join("data", "sensor_data.json"),
                        help="温湿度数据文件路径 (默认 data/sensor_data.json)")
    parser.add_argument("--device-id", default="FILE-TERM-01", help="终端设备 ID")
    parser.add_argument("--deadband", type=float, default=0.01,
                        help="变化阈值，超过才上报 (默认 0.01)")
    parser.add_argument("--host", default=None, help="覆盖 MQTT 服务器地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖 MQTT 服务器端口")
    args = parser.parse_args()

    reporter = MqttReporter(args.device_id, "file",
                            host=args.host, port=args.port)
    reporter.start()

    handler = SensorFileHandler(reporter, args.file, args.deadband)
    file_path = handler.file_path

    if not os.path.exists(file_path):
        log.error("数据文件不存在: %s  (可先创建该文件)", file_path)
        sys.exit(1)

    observer = Observer()
    observer.schedule(handler, os.path.dirname(file_path) or ".", recursive=False)
    observer.start()
    log.info("文件监听终端已启动  设备ID=%s  监听文件=%s", args.device_id, file_path)
    log.info("MQTT 服务器 %s:%d  数据主题 %s",
             args.host or config.MQTT_HOST, args.port or config.MQTT_PORT,
             config.data_topic(args.device_id))

    try:
        # 启动后先上报一次当前值，同时周期性兜底轮询（防止个别编辑器写法监听不到）
        handler.read_and_report(force=True)
        while True:
            time.sleep(5)
            handler.read_and_report()
    except KeyboardInterrupt:
        log.info("收到退出信号")
    finally:
        observer.stop()
        observer.join()
        reporter.stop()


if __name__ == "__main__":
    main()
