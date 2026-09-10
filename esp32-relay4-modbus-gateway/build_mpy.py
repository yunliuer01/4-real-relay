# -*- coding: utf-8 -*-
"""把 main.py 预编译成 app_main.mpy —— 板子实际加载的产物。

为什么必须预编译（2026-09-10 定案，实测数据）：
  ESP32-C3 + MicroPython v1.24.0，boot.py 预连 WiFi 后 gc.mem_free() ≈ 158KB。
  pyexec_file 直接编译源码的峰值 ≈ 2x 源码（源码字节串 + 解析树 + 字节码三者
  同时驻留），所以源码上限约 80KB：
      v6.0.4  main.py = 79857 B  -> 能起（贴着天花板）
      v6.0.5  main.py = 97457 B  -> MemoryError（先是 allocating 1768 bytes，
                                     后是 REPL 里 f.read() allocating 64000 bytes）
  预编译 .mpy 后只剩字节码，源码字符串与解析树都不需要：
      app_main.mpy = 27777 B（源码 28.5%）
      实板实测 import 成本 39760 B，加载后剩余堆 118144 B（3x 余量）
  副作用好处：main.py 的可增长空间从 ~80KB 抬到 ~290KB。

版本强约束：
  .mpy 头里带 version + 架构/特性字节，与固件不匹配会直接拒绝加载
  （ValueError: incompatible .mpy file）。板子固件是 LOLIN_C3_MINI v1.24.0
  （sys.implementation._mpy == 12038），因此必须用 mpy-cross **1.24.0**。
  本脚本会校验工具链版本，不匹配就报错退出。

用法：
  python build_mpy.py            # 编译 + 打印体积报告
  python build_mpy.py --check    # 只校验工具链版本
  python build_mpy.py -q         # 安静模式（只输出一行结果）
"""
from __future__ import print_function

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "main.py")
ENTRY = os.path.join(HERE, "main_entry.py")
OUTPUT = os.path.join(HERE, "app_main.mpy")

# 板子固件：MicroPython v1.24.0 on 2024-10-25; LOLIN_C3_MINI with ESP32-C3FH4
EXPECT_MICROPYTHON = "v1.24.0"
EXPECT_MPY_VERSION = 6          # mpy-cross 1.24.0 报 "mpy v6.3"
EXPECT_MPY_MAJOR_BYTE = 0x06    # .mpy 头第 2 字节
MAX_ENTRY_BYTES = 1200          # 引导壳必须足够小（它才是被直接编译的那份源码）
RATIO_WARN = 0.40               # .mpy / 源码 体积比告警线

CANDIDATES = [
    os.environ.get("MPY_CROSS"),
    shutil.which("mpy-cross"),
    r"C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Scripts\mpy-cross.exe",
    os.path.expanduser("~/.workbuddy/binaries/python/envs/default/bin/mpy-cross"),
]


def find_mpy_cross():
    for c in CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def run(cmd):
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def check_toolchain(mpyc, quiet=False):
    rc, out = run([mpyc, "--version"])
    if rc != 0:
        raise SystemExit("[fatal] mpy-cross --version 失败: %s" % out.strip())
    line = out.strip().splitlines()[0]
    if not quiet:
        print("[tool] %s" % line)
    if EXPECT_MICROPYTHON not in line:
        raise SystemExit(
            "[fatal] mpy-cross 版本不匹配：需要 %s（板子固件 LOLIN_C3_MINI v1.24.0），"
            "实际 %s\n        装法: pip install mpy-cross==1.24.0.post2"
            % (EXPECT_MICROPYTHON, line))
    if ("mpy v%d" % EXPECT_MPY_VERSION) not in line:
        raise SystemExit("[fatal] 预期输出 mpy v%d，实际: %s" % (EXPECT_MPY_VERSION, line))
    return line


def main():
    quiet = "-q" in sys.argv
    mpyc = find_mpy_cross()
    if not mpyc:
        raise SystemExit(
            "[fatal] 找不到 mpy-cross。\n"
            "        装法: pip install mpy-cross==1.24.0.post2\n"
            "        或用环境变量 MPY_CROSS 指定路径")

    check_toolchain(mpyc, quiet)
    if "--check" in sys.argv:
        print("[ok] 工具链版本匹配 %s" % EXPECT_MICROPYTHON)
        return 0

    if not os.path.exists(SOURCE):
        raise SystemExit("[fatal] 缺 %s" % SOURCE)
    if not os.path.exists(ENTRY):
        raise SystemExit("[fatal] 缺 %s（引导壳，会上传为板子 /main.py）" % ENTRY)

    rc, out = run([mpyc, "-o", OUTPUT, SOURCE])
    if rc != 0:
        raise SystemExit("[fatal] mpy-cross 编译失败: %s" % out.strip())

    src_b = os.path.getsize(SOURCE)
    ent_b = os.path.getsize(ENTRY)
    out_b = os.path.getsize(OUTPUT)
    head = open(OUTPUT, "rb").read(2)
    if head[0] != 0x4D or head[1] != EXPECT_MPY_MAJOR_BYTE:
        raise SystemExit("[fatal] 产物头异常: %r" % head)
    if ent_b > MAX_ENTRY_BYTES:
        raise SystemExit("[fatal] 引导壳 %d B 超过 %d B —— 它才是被直接编译的源码"
                         % (ent_b, MAX_ENTRY_BYTES))

    ratio = 100.0 * out_b / src_b
    if quiet:
        print("app_main.mpy %d B (%.1f%% of %d B source)" % (out_b, ratio, src_b))
    else:
        print("[build] main.py        %8d B" % src_b)
        print("[build] main_entry.py  %8d B  (上传为板子 /main.py，被直接编译的就是它)" % ent_b)
        print("[build] app_main.mpy   %8d B  (%.1f%% of 源码)" % (out_b, ratio))
        print("[build] 预估加载成本   ~%d B（约源码的 40.8%%，实板实测口径）" % int(src_b * 0.408))
        print("[build] 预估编译峰值   ~%d B（源码直编口径 2x，已不再走这条路）" % (2 * src_b))
    if ratio > RATIO_WARN * 100:
        print("[warn] .mpy 体积比 %.1f%% 偏高（>%.0f%%），留意板子堆余量"
              % (ratio, RATIO_WARN * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
