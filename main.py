# -*- coding: utf-8 -*-
"""8 路继电器模拟终端 —— 主入口

运行：python main.py [--interval 5] [--device RELAY8-TERM-01] [--product relay8_lfx]
依赖：core/simulator.py（RelaySimulator）、core/channel_bank.py、config.py
前置：产品/设备/EMQX 规则已由 setup/ 下脚本创建，见各脚本 docstring
"""
import argparse
import logging
import time

import config
from core.simulator import RelaySimulator


def main():
    parser = argparse.ArgumentParser(
        description="8路继电器模拟终端(Day3/4, JetLinks relay8_lfx)")
    parser.add_argument("--device", default=config.DEVICE_ID,
                        help=f"设备ID(默认 {config.DEVICE_ID})")
    parser.add_argument("--product", default=config.PRODUCT_ID,
                        help=f"产品ID(默认 {config.PRODUCT_ID})")
    parser.add_argument("--host", default=None, help="MQTT地址(默认取config)")
    parser.add_argument("--port", type=int, default=None, help="MQTT端口(默认取config)")
    parser.add_argument("--user", default=None, help="MQTT用户名(默认取config)")
    parser.add_argument("--passwd", default=None, help="MQTT密码(默认取config)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="定时上报秒数(默认5)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    sim = RelaySimulator(args.device, args.product, args.host, args.port,
                         args.user, args.passwd, args.interval)
    try:
        sim.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.getLogger("relay8.main").info("收到退出信号")
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
