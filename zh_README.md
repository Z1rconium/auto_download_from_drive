# auto_download_from_drive（rclone 自动增量下载守护进程）

[English](./README.md)

长期运行的后台守护进程，持续监控 **rclone 挂载的远端目录**，自动将**仅新出现的文件**下载到本地目标目录。内置 Web 管理面板，通过 WebSocket 推送实时传输进度。

---

## 架构

两个组件之间无任何 IPC，仅通过磁盘文件通信：

```
[ Caddy（TLS 反向代理） ]
         ↓ :5000
[ web-panel.service（Gunicorn / gevent，单 worker） ]
         ↓ 读取
[ /opt/sync/  config.json · sync_state.json · active_transfers.json · sync.log ]
         ↑ 写入
[ sync.service（sync_daemon.py） ]
         ↓ rclone copy
[ rclone-pikpak.service（FUSE 挂载） ]
         ↓
[ 远端云存储（如 PikPak） ]
```

| 组件 | 职责 |
|---|---|
| `sync_daemon.py` | 定时扫描挂载路径，将新文件加入队列，通过 `rclone copy` 下载，持久化状态 |
| `web_panel/app.py` | Flask + SocketIO 面板；除写入 `config.json`（触发守护进程重启）外为只读 |

---

## 目录结构

```
/
├── sync_daemon.py          ← 守护进程入口
├── sync_daemon.service     ← systemd 单元模板（开发用）
├── config.json             ← 共享运行时配置
├── sync_state.json         ← 文件状态持久化（由守护进程写入）
├── active_transfers.json   ← 实时下载注册表（由守护进程写入）
├── sync.log                ← 滚动日志（由守护进程写入）
├── start.sh                ← 一键安装脚本（需要 root）
web_panel/
├── app.py                  ← Flask 应用
├── rclone_monitor.py       ← 轮询 rclone RC 端口获取单任务进度
├── requirements.txt
└── templates/index.html    ← 单页 UI（Tailwind CSS，深色/浅色，SocketIO）
```

---

## 特性

### 增量下载与基线管理
首次启动（或新增规则时），源目录中已存在的文件全部标记为 `baseline` 并跳过。此后**仅处理新出现**的文件，避免重复下载历史数据。

### 多规则独立监控
在 `config.json` 中配置多条独立的 `source_path → dest_path` 规则。每条规则有独立的 `enabled` 开关，可随时切换，无需重启守护进程。

### 并发下载与限速
- 通过 `max_concurrent_downloads` 配置下载 worker 线程数。
- 通过 `bandwidth_limit_mbps` 设置单任务限速（`0` 表示不限）。

### 自动重试
下载失败的文件在后续扫描周期中自动重新入队，直到超过 `max_retry_count` 后标记为 `permanent_failed`。

### 挂载自动刷新
定期重启 rclone 挂载的 systemd 服务，防止挂载失效。刷新前守护进程暂停扫描并等待所有 worker 空闲，重启后确认挂载可用再恢复运行。

### 状态持久化与自动清理
所有文件状态通过 `sync_state.json` 在重启后保留（损坏时自动从 `.bak` 恢复）。源端删除的文件，其状态记录自动清除——本地已下载的副本不受影响。

### Web 管理面板
浏览器 UI 支持：
- 实时单任务传输进度（SocketIO 推送，1 秒间隔）
- 配置编辑（写入 `config.json` 并触发守护进程重启）
- 日志实时查看
- 文件状态概览与统计

### 优雅关闭
收到 SIGTERM 后，守护进程排空待处理队列，最多等待 300 秒让传输中的任务完成，再保存状态后退出。

---

## 文件状态流转

```
（启动时已存在）  → baseline          ← 跳过，永不下载
（新检测到）      → pending
pending → 下载成功 → synced
pending → 下载失败 → failed
failed  → 超过重试上限 → permanent_failed  ← 需手动处理
```

状态以 `<rule_id>:<source_file_path>` 为键存储于 `sync_state.json`。

---

## 运行环境

- Linux + systemd（推荐 Debian/Ubuntu）
- Python 3.11+
- 已安装并配置好的 `rclone`
- 由 systemd 管理的现有 rclone FUSE 挂载服务（如 `rclone-pikpak.service`）
- 初始安装需要 root 权限

---

## 安装

以 root 身份运行内置安装脚本，脚本将自动完成：
1. 停止并移除旧版安装
2. 将项目文件复制到 `/opt/sync/`
3. 创建专用 `web-panel` 系统用户
4. 在 virtualenv 中安装 Python 依赖
5. 写入并启用 `sync.service` 和 `web-panel.service`
6. 在 `/opt/sync/web_panel/.env` 中生成占位 `WEB_PANEL_API_KEY`

```bash
sudo ./start.sh
```

### 安装后必须完成的配置

**1. `/opt/sync/config.json` — 守护进程核心配置**

填写实际的源路径和目标路径，然后将规则设为启用：

```bash
sudo nano /opt/sync/config.json
sudo systemctl restart sync.service
```

**2. `/opt/sync/web_panel/.env` — Web 面板认证配置**

设置强密码 API Key 以及公网域名（用于 CORS）：

```bash
sudo nano /opt/sync/web_panel/.env
sudo systemctl restart web-panel.service
```

**3. Caddy 反向代理示例**

```caddy
panel.example.com {
    @allowed remote_ip 你的.IP.地.址
    handle @allowed {
        reverse_proxy 127.0.0.1:5000
    }
    respond 403
}
```

---

## 配置说明（`config.json`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `scan_interval_seconds` | int | 扫描源目录新文件的间隔（秒） |
| `rclone_refresh_interval_seconds` | int | 重启 rclone 挂载服务的间隔（秒） |
| `max_concurrent_downloads` | int | 并发下载 worker 线程数 |
| `max_retry_count` | int | 标记为 `permanent_failed` 前的最大重试次数 |
| `bandwidth_limit_mbps` | float | 下载限速（Mbps）；`0` 表示不限 |
| `rclone_command` | string | rclone 可执行文件路径或名称（默认：`"rclone"`） |
| `rclone_service_name` | string | rclone FUSE 挂载的 systemd 服务名（如 `"rclone-pikpak"`） |
| `rules` | array | 同步规则列表（见下） |

### 规则字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_path` | string | rclone 挂载的源目录绝对路径 |
| `dest_path` | string | 本地下载目标目录绝对路径 |
| `enabled` | bool | 设为 `true` 以激活该规则 |

---

## Web 面板环境变量（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEB_PANEL_API_KEY` | _（空 = 开发模式）_ | 生产环境必须设置 |
| `WEB_PANEL_ALLOWED_ORIGINS` | `http://localhost,https://localhost` | 逗号分隔的 CORS + SocketIO 允许来源 |
| `WEB_PANEL_SECRET_KEY` | 自动生成 | 显式设置可在服务重启后保持 session 有效 |
| `WEB_PANEL_SESSION_TTL_SECONDS` | `1800` | 滑动 session 有效期（秒） |
| `WEB_PANEL_LOG_LEVEL` | `INFO` | Python 日志级别 |

---

## 常用命令

```bash
# 查看守护进程日志
sudo journalctl -u sync.service -f

# 查看 Web 面板日志
sudo journalctl -u web-panel.service -f
sudo tail -f /var/log/web-panel/error.log

# 重载配置（无需重新安装）
sudo systemctl restart sync.service

# 查看实时文件状态
cat /opt/sync/sync_state.json | python3 -m json.tool
```

---

## 注意事项

- **单向同步。** 守护进程只负责下载新文件，不删除本地副本，不同步删除操作，不执行双向同步。
- **源端删除。** 源端文件消失后，其状态记录自动清除，本地已下载的副本保留不变。
- **权限。** 运行用户需对 `source_path` 有读权限，对 `dest_path` 有写权限。
