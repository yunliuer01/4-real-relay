# archive/ — 会话调试产物归档

本目录内容**不进版本库**（见根目录 `.gitignore` 的 `/archive/*`），只在本机保留，供事后追溯。
注意 `.gitignore` 里写的是 `/archive/*` 而不是 `/archive/`：后者会忽略整个目录，
使紧随其后的 `!/archive/README.md` 取反失效，连本说明文件都进不了库。

## 为什么会有这个目录

2026-09-11 做了一次项目整理。此前仓库根目录堆了 **338 个文件**，其中 326 个是调试期的
一次性产物，把真正的项目结构淹没了。整理原则是**归档而不是删除**——这些文件记录了
v4 → v6.0.5 的完整排查过程，将来复盘故障时可能还要翻，所以原地保留、只是挪了个位置。

整理前后的对比：

| | 整理前 | 整理后 |
|---|---|---|
| 根目录文件 | 338 个 | 5 个（README / config.py / main.py / requirements.txt / .gitignore） |
| 归档文件 | — | 363 个，28 MB |

## 目录结构

```
archive/session-debug-2026-09/
├── logs/               112 个  板级串口日志、验证输出、抓包文本（_board_log_*.txt、_verify_v605_run*.txt …）
├── scripts/            192 个  一次性探针/修复脚本（_board_*.py、_diag_*.py、_repl_*.py、_exp*_main.py …）
├── images/              13 个  排查时截的界面/示波截图（_diag_*.png）
├── board-vfs-backup/     8 个  板载文件系统与分区表原始转储（_board_vfs_backup_*.bin 等，11 MB）
├── snapshots/           30 个  _snapshot/（JetLinks 平台快照）、_mpytest/、_board_backup_v54/、__pycache__/
└── misc/                 8 个  cfg.json、平台设备备份、JetLinks 前端 bundle 等杂项
```

## 里面有什么值得留意的

- **`board-vfs-backup/_board_vfs_backup_20260910c.bin`** — v6.0.5 发布时板子的完整文件系统
  快照，含当时生效的 `/config.json`。要对比「板子上的配置到底是什么样」，看这个。
- **`snapshots/_board_backup_v54/`** — v5.4 时期的板载五件套（`main.py` / `boot.py` /
  `modbus_master.py` / `config.json` / `portal.html`）源码备份，是回滚 v5.x 的参考。
- **`logs/_verify_v605_run1..6.txt`** — v6.0.5 实板并发阶梯验证的 6 次原始输出。
- **`scripts/_verify_v605.py`** — ⚠️ **这个已经「转正」了**，见下。

## 已转正的文件

`_verify_v605.py`（实板 HTTP 并发冻结验证，12/12 PASS 的判据实现）不再只是会话产物，
已复制为受版本管理的 **`esp32-relay4-modbus-gateway/verify_board.py`**。
需要跑实板验收时请用那个路径。

## 怎么找回文件

归档用的是 `mv`，没有再压缩，所有文件都在原路径 `archive/session-debug-2026-09/<类别>/`
下，直接 `ls` / 打开即可。若确定某批文件永远用不上了，可以自行删除整个 `archive/` 目录
——它不影响仓库的任何构建或运行。
