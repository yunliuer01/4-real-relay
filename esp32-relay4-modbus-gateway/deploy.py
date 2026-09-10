# -*- coding: utf-8 -*-
"""v6.0.5 起的一键部署：构建 .mpy + 全量上传 + 软复位抓启动日志。

部署布局（重要）：
    main_entry.py   --(改名)-->   板子 /main.py      被直接编译的引导壳（~1KB）
    main.py         --(mpy-cross)--> 板子 /app_main.mpy  预编译字节码（被 import）
    boot.py / wifi_boot.py / modbus_master.py / modbus_tcp_master.py
    portal_page.html --(改名)-->  板子 /portal.html   （main.py 里写死的是 portal.html）
    config.json 留在板子上不动

为什么不能只传 main.py 源码：见 build_mpy.py 顶部说明与 ROOTCAUSES.md 第 10 条。

用法:
    python deploy.py                # 构建 + 上传 + 复位抓 30s 日志
    python deploy.py -c 60          # 抓 60s
    python deploy.py --no-reset     # 只上传不复位
    python deploy.py --skip-build   # 用现成的 app_main.mpy
"""
from __future__ import print_function

import base64
import os
import subprocess
import sys
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))

COM = os.environ.get("RELAY_COM", "COM3")
BAUD = 115200
CHUNK = 3500
DONE = b"__DONE__"

# (本地文件, 板子上叫什么)
FILES = [
    (os.path.join(HERE, "main_entry.py"), "main.py"),
    (os.path.join(HERE, "app_main.mpy"), "app_main.mpy"),
    (os.path.join(HERE, "boot.py"), "boot.py"),
    (os.path.join(HERE, "wifi_boot.py"), "wifi_boot.py"),
    (os.path.join(HERE, "modbus_master.py"), "modbus_master.py"),
    (os.path.join(HERE, "modbus_tcp_master.py"), "modbus_tcp_master.py"),
    (os.path.join(HERE, "portal_page.html"), "portal.html"),
]

# 调试期留在板上的临时文件，部署时顺手清掉
STALE = ["_mprobe.mpy", "_tmoddef.mpy", "_tmodrv.mpy", "_probe.py"]


def log(m):
    print(m, flush=True)


def arg_int(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


CAPTURE_S = arg_int("-c", 30)
DO_RESET = "--no-reset" not in sys.argv
# 【2026-09-10 修】原写法 `= "--skip-build" not in sys.argv` 是**反的**：
# 不传这个 flag 时它求值为 True，于是 `if not SKIP_BUILD:` 永远为假 ——
# 默认从不构建，一直上传旧 app_main.mpy。这正是"改了代码板上没生效"的根源，
# 也让我加在 build() 里的新鲜度守卫成了摆设（build() 根本没被执行）。
SKIP_BUILD = "--skip-build" in sys.argv


def build():
    log("[1] 构建 app_main.mpy ...")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "build_mpy.py")])
    if rc != 0:
        log("[fatal] 构建失败")
        sys.exit(1)


def check_fresh():
    """上传前的硬关卡：产物必须比它依赖的两份源码都新。

    **必须放在 build() 之外**，否则 `--skip-build` 时它会跟着被跳过
    （2026-09-10 实际踩到：守卫写在 build() 里，而 build() 因上方逻辑反了
    从未执行 → 守卫形同虚设，板上跑了两轮旧字节码）。
    宁可硬失败，也不要默默上传过期产物 —— 那种坑板子日志一切正常，
    只是逻辑是上一版，极难排查。
    """
    built = os.path.join(HERE, "app_main.mpy")
    if not os.path.exists(built):
        log("[fatal] 缺 %s，请先构建" % built)
        sys.exit(1)
    t_out = os.path.getmtime(built)
    for name in ("main.py", "main_entry.py"):
        src = os.path.join(HERE, name)
        if os.path.getmtime(src) > t_out + 1e-6:
            log("[fatal] %s 比 app_main.mpy 新 -> 产物过期，拒绝上传" % name)
            log("        请先重跑 build_mpy.py（或检查它是否真的在编译）")
            sys.exit(1)
    log("[1] 产物已校验：app_main.mpy %d B（比 main.py / main_entry.py 新）"
        % os.path.getsize(built))


class Board(object):
    def __init__(self, com):
        self.s = serial.Serial(com, BAUD, timeout=0.3, write_timeout=3,
                               dsrdtr=False, rtscts=False)
        self.s.setDTR(False)
        self.s.setRTS(False)
        time.sleep(0.5)
        self.s.reset_input_buffer()

    def drain(self, t=1.0):
        b = b""
        end = time.time() + t
        while time.time() < end:
            d = self.s.read(4096)
            if d:
                b += d
        return b

    def to_repl(self, retries=90):
        """尽力打断进 REPL。

        main.py 若正卡在阻塞式 socket.connect()（链路差时可达 ~55s），VM 要到
        该调用返回后才处理 stdin —— 期间发的 0x03 会排队，返回瞬间生效。
        所以重试次数要够（90 次 ≈ 80s），别因「前几秒没反应」就放弃。
        """
        for _ in range(retries):
            try:
                self.s.write(b"\x03")
            except Exception as e:
                log("  ctrl-C write err %r" % (e,))
            time.sleep(0.25)
            b = self.drain(0.6)
            if b">>>" in b:
                return True
            if b"raw REPL" in b:
                self.s.write(b"\x02")
                time.sleep(0.3)
                self.drain(0.5)
        return False

    def raw(self, code, timeout=15):
        if DONE.decode() not in code:
            code += "\nprint('__DONE__')"
        self.s.write(code.encode("utf-8") + b"\x04")
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            d = self.s.read(4096)
            if d:
                buf += d
                if DONE in buf:
                    break
        ok = DONE in buf and b"Traceback" not in buf[buf.rfind(DONE):]
        return ok, buf

    def upload(self, local, remote):
        data = open(local, "rb").read()
        b64 = base64.b64encode(data)
        nchunk = (len(b64) + CHUNK - 1) // CHUNK
        log("[up] %-22s %7d B -> %2d chunks" % (remote, len(data), nchunk))
        ok, out = self.raw("f=open('%s','wb')" % remote)
        if not ok:
            log("[up] open fail %r" % out[-160:])
            return False
        for i in range(0, len(b64), CHUNK):
            ok, out = self.raw("f.write(ubinascii.a2b_base64('%s'))"
                               % b64[i:i + CHUNK].decode())
            if not ok:
                log("[up] chunk %d fail %r" % (i // CHUNK, out[-160:]))
                return False
            time.sleep(0.02)
        self.raw("f.close()")
        ok, out = self.raw("import os; print('SIZE', os.stat('%s')[6])" % remote)
        if (b"SIZE %d" % len(data)) in out:
            return True
        log("[up] SIZE MISMATCH %r" % out[-160:])
        return False


def main():
    if not SKIP_BUILD:
        build()
    check_fresh()            # 无论是否 --skip-build，上传前都必须校验新鲜度

    for local, _ in FILES:
        if not os.path.exists(local):
            log("[fatal] 缺 %s" % local)
            return 1

    b = Board(COM)
    log("[2] (%s) 进 REPL ..." % COM)
    if not b.to_repl():
        log("[fatal] 无法进入 REPL")
        return 2
    log("[3] raw REPL ...")
    b.s.write(b"\x01")
    time.sleep(0.4)
    b.drain(0.8)
    ok, out = b.raw("import os, ubinascii; print('READY')")
    if not ok:
        log("[fatal] raw REPL 未就绪: %r" % out[-200:])
        return 2

    log("[4] 上传 %d 个文件 ..." % len(FILES))
    for local, remote in FILES:
        if not b.upload(local, remote):
            log("[fatal] 上传 %s 失败" % remote)
            return 3

    for stale in STALE:
        ok, out = b.raw("import os\n"
                        "try:\n"
                        "    os.remove('%s')\n"
                        "    print('rm %s')\n"
                        "except OSError:\n"
                        "    print('no %s')" % (stale, stale, stale))
        if b"rm " in out:
            log("[5] 清理临时文件 %s" % stale)

    ok, out = b.raw("import os; print('FINAL', sorted(os.listdir()))")
    log("[6] 板子文件: %r" % out[-260:])

    if not DO_RESET:
        log("[7] --no-reset，跳过复位")
        b.s.close()
        return 0

    log("[7] 软复位 + 抓 %ds 启动日志 ..." % CAPTURE_S)
    b.s.write(b"\x02")
    time.sleep(0.4)
    b.drain(0.8)
    b.s.write(b"\x04")
    lines = []
    end = time.time() + CAPTURE_S
    while time.time() < end:
        d = b.s.read(4096)
        if d:
            for ln in d.decode("utf-8", "backslashreplace").splitlines():
                if ln.strip():
                    lines.append(ln)
                    log("    " + ln)
    log("=" * 60)
    joined = "\n".join(lines)
    hits = [k for k in ("[MAIN]", "MQTT connected", "MQTT CONNECTED",
                        "preflight", "MemoryError", "FATAL") if k in joined]
    log("[8] 关键词命中: %s" % (hits or "无"))
    if "MemoryError" in joined:
        log(">>> 结果: MemoryError ❌")
        return 1
    if "MQTT connected" in joined or "[MAIN]" in joined:
        log(">>> 结果: 固件启动 ✅")
        b.s.close()
        return 0
    log(">>> 结果: 未见启动标志（%d 行日志）❌" % len(lines))
    b.s.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
