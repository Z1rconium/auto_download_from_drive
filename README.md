# auto_download_from_drive（rclone 自动增量下载守护进程）

该项目用于长期运行监控 rclone 挂载目录，只下载**新出现**的文件到本地目标目录。

## 特性

本脚本围绕“**长期守护 + 只拉新文件 + 可恢复运行**”设计，核心特性如下。

### 1) 只下载新文件（避免历史回灌）

- 首次启动会对每条启用规则执行 **bootstrap 基线扫描**。
- 启动时源目录中已存在的文件会被标记为 `baseline`，不会触发下载。
- 后续只处理“扫描中新出现”的文件，适合长期增量同步场景。

### 2) 多规则独立监控

- 支持配置多条规则：`source_path -> dest_path`。
- 每条规则可通过 `enabled` 独立启停，便于分批上线。
- 规则状态按 `rule_<index>` 持久化，互不干扰。

### 3) 周期增量扫描 + 自动入队

- 按 `scan_interval_seconds` 周期扫描启用规则。
- 检测到新文件后自动写入状态并加入下载队列。
- 扫描与下载解耦，发现与执行分离，运行更稳定。

### 4) 并发下载与限速控制

- 支持多线程并发下载（`max_concurrent_downloads`）。
- 下载命令为 `rclone copy <source_file> <dest_path>`。
- 支持带宽限制（`bandwidth_limit_mbps`，`0` 表示不限速），可减少对链路和磁盘的冲击。

### 5) 失败重试与永久失败标记

- 下载失败会进入重试流程，支持可配置上限（`max_retry_count`）。
- 达到上限后标记为 `permanent_failed`，避免无限重试。
- 保留 `last_error`、`last_attempt`、`retry_count` 便于快速定位问题。

### 6) rclone 挂载自动刷新（含扫描保护）

- 按 `rclone_refresh_interval_seconds` 定期执行 `systemctl restart <rclone_service_name>`。
- 刷新期间会暂停扫描，并等待下载队列空闲后再执行刷新流程。
- 刷新后会进行挂载可用性探测，减少“挂载假在线”导致的误扫描。

### 7) 持久化状态与异常恢复

- 所有文件状态持久化到 `sync_state.json`，重启后可继续运行。
- 状态覆盖 `baseline/pending/failed/synced/permanent_failed` 全生命周期。
- 保存状态时维护 `sync_state.json.bak` 备份；主状态损坏时可自动尝试恢复。

### 8) 优雅停止与数据一致性

- 收到 `SIGINT/SIGTERM` 后进入优雅退出流程。
- 会等待进行中的下载任务完成，再落盘状态并退出。
- 避免强退导致的状态丢失和重复下载。

### 9) 结构化日志与可观测性

- 同时输出到 stdout 与 `sync.log`。
- 文件日志启用轮转（10MB × 5），长期运行更可控。
- 事件按类型分层（`SCAN` / `DOWNLOAD` / `REFRESH` / `ERROR` / `SYSTEM`），排障路径清晰。

### 10) systemd 友好部署

- 提供 `sync_daemon.service` 示例，可直接纳入系统守护。
- 支持 `status/restart/stop/journalctl` 标准运维操作。
- 适合无人值守、开机自启、长期在线运行。

### 11) 配置驱动与边界清晰

- 关键行为均由 `config.json` 驱动，参数集中、可审计。
- 明确聚焦“新文件下载”单职责：
  - 不做双向同步；
  - 不联动源端删除/重命名；
  - 默认不基于已记录文件的内容变更做二次同步。

### 12) 状态自动清理（跟随源端实时变化）

- 每次增量扫描会对比 `sync_state.json` 与当前 `source_path` 的实际文件列表。
- 已从源端消失的文件，其状态条目会被**自动移除**，保持状态文件与源端实时一致。
- 扫描日志中新增 `removed_files` 字段，便于观察每周期的清理情况。
- 注意：清理的是**状态记录**，不会删除已下载到 `dest_path` 的本地文件。

---

## 目录与文件

- `sync_daemon.py`：主程序。
- `config.json`：运行配置文件。
- `sync_state.json`：持久化状态（运行后自动创建/更新）。
- `sync.log`：运行日志（自动创建，轮转）。
- `sync_daemon.service`：systemd 服务文件示例。

---

## 运行环境

- Linux + systemd（建议 Debian/Ubuntu 系）。
- Python 3.11+（系统 Python 亦可）。
- 已安装并可执行的 `rclone`。
- 已存在可重启的 rclone 挂载服务（如 `rclone-pikpak`）。

---

## 快速开始（手动运行）

```bash
cd /opt/auto_download_from_drive
python3 sync_daemon.py
```

首次运行如果 `config.json` 不存在，会自动生成模板并退出；按需修改后再次启动。

---

## 配置说明（config.json）

| 字段 | 类型 | 说明 |
|---|---|---|
| `scan_interval_seconds` | int | 增量扫描周期（秒），必须 > 0 |
| `rclone_refresh_interval_seconds` | int | 挂载刷新周期（秒），必须 > 0 |
| `max_concurrent_downloads` | int | 并发下载线程数，必须 > 0 |
| `max_retry_count` | int | 单文件最大重试次数，必须 >= 0 |
| `bandwidth_limit_mbps` | number | 下载限速（MB/s），`0` 表示不限速 |
| `rclone_command` | string | rclone 命令名或绝对路径 |
| `rclone_service_name` | string | 刷新时重启的 systemd 服务名 |
| `rules` | list | 同步规则列表 |

`rules` 中每个规则：

- `source_path`：源目录（通常是 rclone 挂载目录）
- `dest_path`：目标目录
- `enabled`：是否启用

> 建议：先保证 `source_path` 可访问、`dest_path` 可写，再把 `enabled` 设为 `true`。

---

## 状态文件说明（sync_state.json）

状态按规则保存，规则 ID 形如 `rule_0`、`rule_1`（来自 `rules` 数组下标）。

单文件状态字段主要包括：

- `status`：
  - `baseline`：启动基线文件（不下载）
  - `pending`：待下载
  - `failed`：下载失败，待重试
  - `synced`：下载成功
  - `permanent_failed`：达到最大重试次数，不再自动重试
- `retry_count`：当前重试计数
- `last_error`：最后一次错误
- `last_attempt`：最后一次尝试时间（UTC ISO8601）
- `last_seen`：最近一次扫描看到该文件的时间
- `size` / `mtime_ns`：首次记录时的文件大小与时间戳

程序会在保存状态时维护 `sync_state.json.bak` 作为备份。

---

## 工作流程

1. 检查 `config.json`（不存在则生成模板并退出）。
2. 加载 `sync_state.json`（损坏时尝试从 `.bak` 恢复）。
3. 对启用规则执行 bootstrap 扫描，记录现有文件为 `baseline`。
4. 主循环中：
   - 到达刷新周期：暂停扫描，等待下载队列空闲，重启挂载服务并探测挂载恢复。
   - 到达扫描周期：增量扫描新文件并入队；同时把可重试文件重新入队；并清理源端已不存在文件的状态条目。
5. 下载线程执行：
   - `rclone copy <source_file> <dest_path>`
   - 若配置了 `bandwidth_limit_mbps > 0`，附加 `--bwlimit <N>M`
6. 退出时等待在途任务并持久化状态。

---

## systemd 部署

1. 复制项目到部署目录（示例）：

```bash
sudo mkdir -p /opt/auto_download_from_drive
sudo cp -r ./* /opt/auto_download_from_drive/
```

2. 安装服务文件：

```bash
sudo cp /opt/auto_download_from_drive/sync_daemon.service /etc/systemd/system/sync_daemon.service
sudo systemctl daemon-reload
```

3. 启用并启动：

```bash
sudo systemctl enable --now sync_daemon.service
```

4. 常用命令：

```bash
sudo systemctl status sync_daemon.service
sudo systemctl restart sync_daemon.service
sudo systemctl stop sync_daemon.service
sudo journalctl -u sync_daemon.service -f
```

> 注意：仓库内 `sync_daemon.service` 的 `WorkingDirectory` / `ExecStart` 当前是 `/root/auto_download_from_drive`。如果你部署在 `/opt/auto_download_from_drive`，请先修改服务文件再启动。

---

## 日志说明

日志格式：

- 时间 + 级别 + 事件类型 + JSON 消息体
- 事件类型：`SCAN` / `DOWNLOAD` / `REFRESH` / `ERROR` / `SYSTEM`

查看方式：

- 文件日志：`sync.log`
- systemd 日志：`journalctl -u sync_daemon.service -f`

---

## 常见问题排查

1. **扫描报 source path not ready**
   - 检查挂载是否正常、目录是否存在、服务用户是否有读取权限。

2. **下载失败/重试后仍失败**
   - 查看 `sync.log` / `journalctl` 中的 `ERROR` 明细。
   - 检查 `rclone_command` 是否可执行、目标路径是否可写。

3. **刷新挂载失败**
   - 确认 `rclone_service_name` 正确。
   - 确认运行用户有权限执行 `systemctl restart <service>`。

4. **文件一直不再重试**
   - 可能已到 `max_retry_count`，状态变为 `permanent_failed`。

---

## 重要行为说明

- 只处理"新发现文件"，不会删除目标目录中的旧文件。
- 不做双向同步，也不做源文件删除/重命名联动。
- 程序不会基于文件内容变更做二次同步（已记录文件默认只更新 `last_seen`）。
- 每次扫描会自动清理 `sync_state.json` 中已从源端消失的文件记录，但**不影响 `dest_path` 已下载的文件**。
- 规则 ID 由 `rules` 的数组下标生成；频繁调整规则顺序可能导致状态映射混乱，建议尽量稳定顺序。