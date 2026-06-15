# auto_download_from_drive（轻量化 rclone 增量下载脚本）

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![rclone](https://img.shields.io/badge/rclone-remote%20or%20mount-3F79E0)](https://rclone.org/)
[![systemd](https://img.shields.io/badge/systemd-service-FFB300)](https://systemd.io/)
[![Linux](https://img.shields.io/badge/Linux-Debian%20%2F%20Ubuntu-FCC624?logo=linux&logoColor=black)](https://kernel.org/)

[English](./README.md)

这个项目现在只围绕 [`sync_daemon.py`](./sync_daemon.py) 工作，没有前端，没有反向代理要求。

它是一个单向增量下载守护进程：

- 首次扫描只建立基线，不回补历史文件
- 后续只处理新出现的文件，但会等 size/mtime 连续两次扫描稳定后再下载
- 实际下载用 `rclone copyto`，会在目标目录下保留源端相对路径
- 支持本地挂载目录和直接 rclone remote
- 支持串行下载、重试、带宽限制
- 保留源端子目录，避免同名文件互相覆盖
- 新文件稳定后才会入队下载
- 有下载任务活跃或排队时暂停扫描，等下载空闲后再检测新文件
- 支持 systemd 保活和 watchdog
- 单个 `sync.log` 文件原地裁剪，只保留最近 24 小时记录

这不是双向同步，不是镜像同步，也不会删除目标目录文件。

## 支持的 `source_path`

- 本地挂载路径：`/mnt/pikpak/My Pack`
- 直接 rclone remote：`pikpak:My Pack`

## 项目文件

- `sync_daemon.py`：守护进程主体
- `start.sh`：安装到 `/opt/sync`
- `update.sh`：更新 `/opt/sync/sync_daemon.py` 和 `sync.service`
- `config.json`：运行配置，不存在时自动生成
- `sync_state.json`：状态持久化
- `sync.log`：单个当前日志文件，原地裁剪为最近 24 小时记录

## 安装

```bash
sudo ./start.sh
```

## 配置

编辑 `/opt/sync/config.json`：

```json
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 1,
  "max_retry_count": 5,
  "download_timeout_seconds": 0,
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "",
  "telegram": {
    "enabled": false,
    "bot_token": "",
    "chat_id": "",
    "message_thread_id": null
  },
  "rules": [
    {
      "source_path": "pikpak:My Pack",
      "dest_path": "/data/downloads",
      "enabled": true
    }
  ]
}
```

如果你是直接 remote 模式，`rclone_service_name` 可以留空。

如果你是挂载目录模式，并且想定时刷新挂载，就填对应的 systemd 服务名，比如：

```json
{
  "rclone_service_name": "rclone-pikpak"
}
```

改完重启：

```bash
sudo systemctl restart sync.service
```

## 配置字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `scan_interval_seconds` | int | 增量扫描间隔 |
| `rclone_refresh_interval_seconds` | int | 刷新周期 |
| `max_concurrent_downloads` | int | 为兼容旧配置保留；运行时固定单文件下载 |
| `max_retry_count` | int | 正整数，达到 `permanent_failed` 前的失败阈值 |
| `download_timeout_seconds` | int | 单次下载总超时；`0` 表示不启用守护进程侧总超时 |
| `bandwidth_limit_mbps` | number | `0` 表示不限速，否则传给 `rclone --bwlimit` |
| `rclone_command` | string | `rclone` 命令名或绝对路径 |
| `rclone_service_name` | string | 刷新时要重启的 systemd unit；留空表示不重启服务 |
| `telegram` | object | Telegram Bot 通知配置；每次 `rclone copyto` 成功后发送可读通知 |
| `rules` | array | 下载规则列表 |

Telegram 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `enabled` | bool | 是否启用 Telegram 通知 |
| `bot_token` | string | Telegram Bot API token |
| `chat_id` | string | 目标私聊、群组或频道 id |
| `message_thread_id` | int/null | 可选的论坛话题 id；普通聊天填 `null` |

规则字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_path` | string | 本地挂载路径或直接 rclone remote |
| `dest_path` | string | 本地目标目录 |
| `enabled` | bool | 是否启用；必须是 JSON boolean，不能写成字符串 |
| `id` | string | 可选稳定规则 id；不填时根据 `source_path` + `dest_path` 自动生成 |

## 工作方式

首次运行时：

```text
已有文件 -> baseline
```

规则状态不再使用数组下标作为 id，所以调整 `rules` 顺序不会重置状态。不显式配置 `id` 时，修改规则的 `source_path` 或 `dest_path` 会生成新的自动 id，并为新规则重新建立 baseline。

后续扫描时：

```text
新文件 -> observed -> pending -> synced
                 |
                 -> failed -> permanent_failed
```

`observed` 表示守护进程已经看到新文件或已同步文件的新版本，但还不会立刻下载。只有下一次扫描发现 size/mtime 没变，才会进入 `pending` 并入队。

目标目录结构：

```text
source_path: pikpak:
源文件:      pikpak:Movies/A/movie.mkv
目标文件:    /data/downloads/Movies/A/movie.mkv
```

刷新流程：

1. 等待当前没有活跃下载，也没有排队下载。
2. 暂停新扫描。
3. 如果配置了 `rclone_service_name`，就执行 `systemctl restart <service>`。
4. 轮询所有启用规则，等源端重新可用。
5. 恢复扫描。

## 日志和状态

常用看法：

```bash
sudo journalctl -u sync.service -f
tail -f /opt/sync/sync.log
```

## 本地检查

```bash
python3 -m py_compile sync_daemon.py
python3 -m unittest
python3 sync_daemon.py
```

## 更新

```bash
sudo ./update.sh
```

`update.sh` 会保留 `/opt/sync/config.json`。
