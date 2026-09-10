# -*- coding: utf-8 -*-
"""test_v605_mpy_packaging.py —— v6.0.5 打包链路验收（.mpy 预编译部署）

被测产物：esp32-relay4-modbus-gateway/{main_entry.py, build_mpy.py, deploy.py}
后端：无需 broker / 无需硬件（只做静态检查 + 本地跑一次 mpy-cross）。

背景（2026-09-10 实板实测，详见 memory/ROOTCAUSES.md 第 10 条）：
  ESP32-C3 上 boot.py 预连 WiFi 后可用堆 ≈ 158208 B；pyexec_file 直接编译源码的
  峰值 ≈ 2x 源码（源码字节串 + 解析树 + 字节码同时驻留）-> main.py 源码超过约
  80KB 就 MemoryError 起不来（v6.0.4 的 79857 B 已贴线，v6.0.5 的 97457 B 直接崩）。
  改为部署 mpy-cross 预编译字节码后：app_main.mpy 27777 B，实板 import 成本
  39760 B，加载后剩余堆 118144 B。

这个测试守住的是「打包链路没被改坏」——一旦有人把 main.py 里的逻辑直接改成源码
启动、或忘了把 app_main.mpy 加进部署清单、或用错 mpy-cross 版本，实板就起不来，
而四个仿真台（它们自己 exec 源码）**全都发现不了**。

断言：
  P0 前置：引导壳 main_entry.py 存在且足够小（它才是板上被直接编译的那份源码）
  P1 工具链：mpy-cross 版本必须与板子固件 v1.24.0 匹配（不匹配 -> 板子直接拒绝加载）
  P2 构建：build_mpy.py 跑通且产出 app_main.mpy
  P3 产物头：'M' + mpy 版本字节 6
  P4 体积比：app_main.mpy / main.py <= 40%（实测 28.5%）
  P5 部署映射：main_entry.py -> /main.py、portal_page.html -> /portal.html，且本地文件齐全
  P6 安全余量：预估加载成本 <= 实板可用堆的 60%，且 .mpy <= 40KB
  P7 文档：ROOTCAUSES 第 10 条已记录 .mpy 方案
  P8 一致性：入库的 app_main.mpy 与当前源码重建结果字节一致（防"改了源码忘重建"）
  P9 部署开关：deploy.py 的 --skip-build 语义正确 + 新鲜度守卫独立于 build() 且上传前必调

用法：
    python firmware-simtest/test_v605_mpy_packaging.py
"""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SIM_DIR, ".."))
FW_DIR = os.path.join(PROJ_ROOT, "esp32-relay4-modbus-gateway")

ENTRY = os.path.join(FW_DIR, "main_entry.py")
BUILD = os.path.join(FW_DIR, "build_mpy.py")
DEPLOY = os.path.join(FW_DIR, "deploy.py")
SOURCE = os.path.join(FW_DIR, "main.py")
MPY = os.path.join(FW_DIR, "app_main.mpy")
ROOTCAUSES = os.path.join(PROJ_ROOT, ".workbuddy", "memory", "ROOTCAUSES.md")

BOARD_HEAP_FREE = 158208        # 实板 [wifi-boot] CONNECTED 后 gc.mem_free()，实测
LOAD_RATIO = 0.408              # 实板 import app_main.mpy 成本 / 源码字节数，实测
MAX_ENTRY_BYTES = 1200          # 引导壳上限
RATIO_MAX = 0.40                # .mpy / 源码 体积比上限
MPY_MAX_BYTES = 40000
MARGIN = 0.60                   # 加载成本最多占用可用堆的比例

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  |  " + detail) if detail else ""), flush=True)


def run(cmd, cwd=None):
    p = subprocess.run(cmd, capture_output=True, cwd=cwd)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace") + \
        (p.stderr or b"").decode("utf-8", "replace")


def deploy_mapping():
    """用 ast 抠出 deploy.py 的 FILES 列表，避免为了读常量去 import（会拉起 pyserial）。"""
    tree = ast.parse(open(DEPLOY, encoding="utf-8").read())

    def local_name(node):
        # FILES 里写的是 os.path.join(HERE, "main_entry.py")，取末位字符串常量
        if isinstance(node, ast.Call):
            return node.args[-1].value
        return ast.literal_eval(node)

    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "FILES" for t in node.targets):
            out = []
            for elt in node.value.elts:
                out.append((os.path.join(FW_DIR, local_name(elt.elts[0])), elt.elts[1].value))
            return out
    raise RuntimeError("deploy.py 里找不到 FILES")


def main():
    print("=" * 78)
    print("v6.0.5 打包链路验收（.mpy 预编译部署）")
    print("=" * 78)

    # ---- P0 前置 ----
    entry_src = open(ENTRY, encoding="utf-8").read() if os.path.exists(ENTRY) else ""
    entry_b = len(entry_src.encode("utf-8"))
    check("P0a 引导壳 main_entry.py 存在", bool(entry_src), "%d B" % entry_b)
    check("P0b 引导壳够小（<= %d B；板上被直接编译的就是它）" % MAX_ENTRY_BYTES,
          0 < entry_b <= MAX_ENTRY_BYTES, "%d B" % entry_b)
    check("P0c 引导壳 gc.collect() + import app_main",
          "gc.collect()" in entry_src and "import app_main" in entry_src,
          "两条关键语句都在")
    check("P0d build_mpy.py / deploy.py 就位",
          os.path.exists(BUILD) and os.path.exists(DEPLOY),
          "build=%s deploy=%s" % (os.path.exists(BUILD), os.path.exists(DEPLOY)))

    # ---- P1 工具链 ----
    rc, out = run([sys.executable, BUILD, "--check"])
    check("P1 mpy-cross 版本匹配板子固件 v1.24.0", rc == 0,
          out.strip().replace("\n", " / ")[-150:])
    if rc != 0:
        print("\n===== 打包链路验收中止：工具链不匹配，.mpy 会被板子拒绝 =====")
        return 1

    # ---- P2 构建 ----
    # 必须能证明「构建真的跑了」。最早的做法是把旧产物 os.replace 成 .prev 再构建，
    # 但收尾的 os.remove(.prev) 会被沙箱 safe-delete 拦截
    # （OSError [safe-delete][SAFE_DELETE_FAIL_CLOSED]），留下垃圾文件（2026-09-10 踩到）。
    # 改用 **mtime 哨兵**：build_mpy.py 每次都无条件调用 mpy-cross 重写 -o 目标，
    # 故构建后 mtime 必然前进。若构建被跳过（例如 build_mpy.py 被改成空操作、
    # 或某个守卫提前 return），mtime 不变 -> P2 直接失败。
    # 这样既证明是真重建，又不产生任何需要删除的中间文件。
    pre_hash = None
    pre_mtime = None
    if os.path.exists(MPY):
        with open(MPY, "rb") as f:
            pre_hash = hashlib.sha256(f.read()).hexdigest()
        pre_mtime = os.path.getmtime(MPY)

    rc, out = run([sys.executable, BUILD])
    ok_build = rc == 0 and os.path.exists(MPY)
    if pre_mtime is None:
        rebuilt = ok_build                      # 首次构建：文件本就不存在，产出即真
        how = "首次构建（此前无产物）"
    else:
        rebuilt = ok_build and os.path.getmtime(MPY) > pre_mtime
        how = "mtime 前进 %.3f -> %.3f" % (pre_mtime, os.path.getmtime(MPY) if ok_build else -1)
    check("P2 build_mpy.py 跑通且产物被真正重写（mtime 哨兵，防「构建被跳过」）",
          rebuilt, (out.strip().splitlines()[-1] if out.strip() else "") + " | " + how)
    if not ok_build:
        print(out)
        print("\n===== 打包链路验收：构建失败 =====")
        return 1

    post_hash = hashlib.sha256(open(MPY, "rb").read()).hexdigest()
    if pre_hash is None:
        check("P8 入库的 app_main.mpy 与当前源码重建结果一致（防「改了源码忘重建」）",
              True, "首次构建（入库产物此前不存在，无从比对）")
    else:
        check("P8 入库的 app_main.mpy 与当前源码重建结果一致（防「改了源码忘重建」）",
              pre_hash == post_hash,
              ("指纹一致 %s" % post_hash[:16]) if pre_hash == post_hash
              else ("入库为过期产物：入库 %s != 重建 %s —— 提交前请重跑 build_mpy.py"
                    % (pre_hash[:16], post_hash[:16])))

    src_b = os.path.getsize(SOURCE)
    mpy_b = os.path.getsize(MPY)

    # ---- P3 产物头 ----
    head = open(MPY, "rb").read(2)
    check("P3 .mpy 头 = 'M' + mpy 版本 %d" % 6, head == b"M\x06", repr(head))

    # ---- P4 体积比 ----
    ratio = float(mpy_b) / src_b
    check("P4 体积比 <= %.0f%%（源码 %d B -> .mpy %d B）" % (RATIO_MAX * 100, src_b, mpy_b),
          ratio <= RATIO_MAX, "%.1f%%" % (ratio * 100))

    # ---- P5 部署映射 ----
    mapping = dict((os.path.basename(l), r) for l, r in deploy_mapping())
    check("P5a main_entry.py 上传为板子 /main.py", mapping.get("main_entry.py") == "main.py",
          "实际 %r" % mapping.get("main_entry.py"))
    check("P5b portal_page.html 上传为板子 /portal.html（main.py 里写死的是 portal.html）",
          mapping.get("portal_page.html") == "portal.html",
          "实际 %r" % mapping.get("portal_page.html"))
    check("P5c app_main.mpy 在部署清单里", mapping.get("app_main.mpy") == "app_main.mpy",
          "实际 %r" % mapping.get("app_main.mpy"))
    missing = [os.path.basename(l) for l, _ in deploy_mapping() if not os.path.exists(l)]
    check("P5d 部署清单里的本地文件都存在", not missing, "缺 %s" % missing if missing else "7/7 齐全")

    # ---- P6 安全余量 ----
    est = int(src_b * LOAD_RATIO)
    check("P6a 预估加载成本 %d B <= 实板可用堆 %d B 的 %.0f%%"
          % (est, BOARD_HEAP_FREE, MARGIN * 100),
          est <= BOARD_HEAP_FREE * MARGIN,
          "余量 %.2fx" % (float(BOARD_HEAP_FREE) / est))
    check("P6b app_main.mpy <= %d B" % MPY_MAX_BYTES, mpy_b <= MPY_MAX_BYTES, "%d B" % mpy_b)
    print("      [info] 源码直编口径峰值 ≈ 2x = %d B > 可用堆 %d B，所以源码启动这条路是死的"
          % (2 * src_b, BOARD_HEAP_FREE))

    # ---- P7 文档 ----
    rc_txt = open(ROOTCAUSES, encoding="utf-8").read() if os.path.exists(ROOTCAUSES) else ""
    check("P7 ROOTCAUSES 第 10 条已记录 .mpy 方案",
          "app_main.mpy" in rc_txt and "mpy-cross" in rc_txt,
          "关键词 app_main.mpy / mpy-cross")

    # ---- P9 deploy.py 的两个结构坑（2026-09-10 实锤踩到）----
    # 坑① `SKIP_BUILD = "--skip-build" not in sys.argv` 语义**反了**：
    #      不传该 flag 时求值为 True -> `if not SKIP_BUILD:` 恒假 -> **默认从不构建**，
    #      一直把旧 app_main.mpy 传上去。这就是"改了代码板上没生效"的根源。
    # 坑② 新鲜度守卫原本写在 build() 体内，而 build() 因坑①从未被执行 -> 守卫形同虚设。
    #      守卫必须独立成函数、且由 main() 在**上传前**无条件调用（含 --skip-build）。
    dtree = ast.parse(open(DEPLOY, encoding="utf-8").read())
    _d_funcs = dict((n.name, n) for n in dtree.body if isinstance(n, ast.FunctionDef))

    _skip_ok = False
    for _n in ast.walk(dtree):
        if isinstance(_n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SKIP_BUILD" for t in _n.targets):
            if isinstance(_n.value, ast.Compare):
                _ops = _n.value.ops
                _skip_ok = (any(isinstance(o, ast.In) for o in _ops)
                            and not any(isinstance(o, ast.NotIn) for o in _ops))
    check("P9a deploy.py 的 --skip-build 开关语义正确（是 `in`，写成 `not in` 会默认永不构建）",
          _skip_ok, "SKIP_BUILD = '--skip-build' in sys.argv" if _skip_ok
          else "语义反了或未找到 SKIP_BUILD 赋值")

    _main_fn = _d_funcs.get("main")
    _build_fn = _d_funcs.get("build")
    _fresh_def = "check_fresh" in _d_funcs
    _called_in_main = _main_fn is not None and any(
        isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
        and x.func.id == "check_fresh" for x in ast.walk(_main_fn))
    _nested_in_build = _build_fn is not None and any(
        isinstance(x, ast.FunctionDef) and x.name == "check_fresh"
        for x in ast.walk(_build_fn))
    check("P9b 新鲜度守卫独立于 build() 且在上传前被调用（--skip-build 也拦得住）",
          _fresh_def and _called_in_main and not _nested_in_build,
          "check_fresh: 定义=%s main()内调用=%s 被嵌在build()里=%s"
          % (_fresh_def, _called_in_main, _nested_in_build))

    n_pass = sum(1 for _, ok, _ in CHECKS if ok)
    print()
    print("===== v6.0.5 打包链路验收：%d/%d %s =====" % (
        n_pass, len(CHECKS), "PASS" if n_pass == len(CHECKS) else "FAIL"))
    for name, ok, d in CHECKS:
        if not ok:
            print("  FAILED: %s  %s" % (name, d))
    return 0 if n_pass == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
