# auto_download_from_drive（轻量化 rclone 增量下载脚本）

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![rclone](https://img.shields.io/badge/rclone-remote%20or%20mount-3F79E0)](https://rclone.org/)
[![systemd](https://img.shields.io/badge/systemd-service-FFB300)](https://systemd.io/)
[![Linux](https://img.shields.io/badge/Linux-Debian%20%2F%20Ubuntu-FCC624?logo=linux&logoColor=black)](https://kernel.org/)

[English](./README.md)

这个项目现在只围绕 [`sync_daemon.py`](./sync_daemon.py) 工作，没有前端，没有反向代理要求。

它是一个单向增量下载守护进程：

- 首次扫描只建立基线，不回补历史文件
- 后续只处理新出现的文件
- 实际下载用 `rclone copy`，并默认附加多线程、重试和超时参数
- 支持本地挂载目录和直接 rclone remote
- 支持串行下载、重试、超时、带宽限制
- 有下载任务活跃或排队时暂停扫描，等下载空闲后再检测新文件
- 支持 systemd 保活和 watchdog
- 日志按小时轮转，保留最近约 24 小时

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
- `runtime_status.json`：当前活跃/排队计数
- `active_transfers.json`：当前执行中的 `rclone copy`
- `sync.log`：当前日志文件，按小时轮转

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
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "",
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
| `bandwidth_limit_mbps` | number | `0` 表示不限速，否则传给 `rclone --bwlimit` |
| `rclone_command` | string | `rclone` 命令名或绝对路径 |
| `rclone_service_name` | string | 刷新时要重启的 systemd unit；留空表示不重启服务 |
| `rules` | array | 下载规则列表 |

规则字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_path` | string | 本地挂载路径或直接 rclone remote |
| `dest_path` | string | 本地目标目录 |
| `enabled` | bool | 是否启用；必须是 JSON boolean，不能写成字符串 |

## 工作方式

首次运行时：

```text
已有文件 -> baseline
```

修改规则的 `source_path` 会重置该规则状态，并为新源重新建立 baseline。

后续扫描时：

```text
新文件 -> pending -> synced
             |
             -> failed -> permanent_failed
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
cat /opt/sync/runtime_status.json
cat /opt/sync/active_transfers.json
```

## 本地检查

```bash
python3 -m py_compile sync_daemon.py
python3 sync_daemon.py
```

## 更新

```bash
sudo ./update.sh
```

`update.sh` 会保留 `/opt/sync/config.json`。
