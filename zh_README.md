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
- 新出现的文件夹会作为一个整体同步任务处理，不会给文件夹里的每个文件单独建任务
- 文件和文件夹下载都使用 `rclone copy`，会在目标目录下保留源端相对路径
- 支持本地绝对路径和直接 rclone remote
- 支持可配置并发下载、重试、带宽限制
- 保留源端子目录，避免同名文件互相覆盖
- 新文件稳定后才会入队下载
- 新文件夹稳定后会作为单个文件夹任务入队
- 有下载任务活跃或排队时暂停扫描，等下载空闲后再检测新文件
- 支持 systemd 保活和 watchdog
- 单个 `sync.log` 文件原地裁剪，只保留最近 24 小时记录
- Telegram Bot 菜单支持实时下载进度和规则状态查看
- 每个下载子进程使用独立本地 Rclone RC 端口查询进度，避免默认 `localhost:5572` 冲突

这不是双向同步，不是镜像同步，也不会删除目标目录文件。

## 支持的路径格式

- 本地挂载路径：`/mnt/pikpak/My Pack`
- 直接 rclone remote：`pikpak:My Pack`

`source_path` 和 `dest_path` 都支持这两种格式。

## 项目文件

- `sync_daemon.py`：守护进程主体
- `config.json`：运行配置，不存在时自动生成
- `sync_state.json`：状态持久化
- `sync.log`：单个当前日志文件，原地裁剪为最近 24 小时记录
- `tests/`：单元测试

## 快速开始

### 本地开发运行

直接运行：

```bash
python3 sync_daemon.py
```

如果不存在 `config.json`，守护进程会自动创建。编辑配置文件设置同步规则：

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
    "message_thread_id": null,
    "poll_timeout_seconds": 25,
    "progress_refresh_seconds": 1,
    "progress_live_seconds": 120
  },
  "rclone_rc": {
    "host": "127.0.0.1",
    "port_min": 0,
    "port_max": 0,
    "request_timeout_seconds": 2
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

`dest_path` 也可以直接写 rclone remote，例如 `"pikpak:Downloads"`。

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
| `max_concurrent_downloads` | int | 下载 worker 数；`1` 时 Telegram 显示单任务进度条，大于 `1` 时显示活跃下载列表 |
| `max_retry_count` | int | 正整数，达到 `permanent_failed` 前的失败阈值 |
| `download_timeout_seconds` | int | 单次下载总超时；`0` 表示不启用守护进程侧总超时 |
| `bandwidth_limit_mbps` | number | `0` 表示不限速，否则传给 `rclone --bwlimit` |
| `rclone_command` | string | `rclone` 命令名或绝对路径 |
| `rclone_service_name` | string | 刷新时要重启的 systemd unit；留空表示不重启服务 |
| `telegram` | object | Telegram Bot 配置；用于完成通知、长轮询命令、inline 按钮和进度视图 |
| `rclone_rc` | object | 每个下载任务的本地 Rclone RC 配置，用于查询 `core/stats` 进度 |
| `rules` | array | 下载规则列表 |

Telegram 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `enabled` | bool | 是否启用 Telegram 通知和 Bot 长轮询 |
| `bot_token` | string | Telegram Bot API token |
| `chat_id` | string | 目标私聊、群组或频道 id |
| `message_thread_id` | int/null | 可选的论坛话题 id；普通聊天填 `null` |
| `poll_timeout_seconds` | int | `getUpdates` 长轮询超时；默认 `25` |
| `progress_refresh_seconds` | int | 实时进度消息编辑间隔；默认 `1`；配置大于 1 秒时会按 1 秒执行 |
| `progress_live_seconds` | int | 点击 `Downloading` 后的实时进度开关；默认 `120`，设为 `0` 表示关闭；有活跃或排队下载时会刷新到空闲，确保最终显示 `0/0` |

Rclone RC 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `host` | string | 必须是 `127.0.0.1`；RC 不会绑定到非 loopback 地址 |
| `port_min` | int | 可用 RC 端口下限；和 `port_max` 同为 `0` 时表示自动分配临时本地端口 |
| `port_max` | int | 可用 RC 端口上限；和 `port_min` 同为 `0` 时表示自动分配临时本地端口 |
| `request_timeout_seconds` | int | 守护进程请求每个下载任务 `core/stats` 的超时；默认 `2` |

规则字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_path` | string | 本地绝对路径或直接 rclone remote |
| `dest_path` | string | 本地绝对目标目录或直接 rclone remote |
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

如果检测到新文件夹，守护进程会以这个文件夹作为任务边界。文件夹内的子文件不会逐个入队；等整个文件夹子树连续扫描稳定后，只创建一个文件夹同步任务。

Telegram：

- `/start`、`/status`、`/menu` 会发送同一个 inline 菜单。
- 菜单按钮为 `Downloading` 和 `States`。
- 守护进程只响应配置中的 `chat_id`。
- 如果配置了 `message_thread_id`，发送消息时会继续带上该 topic id。
- `Downloading` 会读取每个活跃下载的 Rclone RC `core/stats`。单 worker 时显示类似 `██████░░░░ 63%` 的文本进度条；多个 worker 时按行显示每个活跃下载的速度和 ETA。
- `States` 会展示所有规则、启用和初始化状态、源/目标路径、文件状态计数，以及最近一次扫描检查结果。

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

## 安全说明

- Rclone RC 只在每个 `rclone copy` 子进程生命周期内启用。
- 每个子进程使用独立的 `--rc-addr 127.0.0.1:<port>`，不会依赖或冲突默认 `127.0.0.1:5572`。
- 守护进程会拒绝非 loopback RC host，也不会追加 `--rc-no-auth`。
- 守护进程创建的运行期文件（`config.json`、`sync_state.json`、`sync.log`）会限制为仅 owner 可读写。
- 不要把 Telegram token、chat id、运行状态、活跃下载文件或日志提交到源码仓库。

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
