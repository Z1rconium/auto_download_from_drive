#!/bin/bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 请使用 root 权限运行: sudo ./start.sh"
    exit 1
fi

INSTALL_DIR="/opt/sync"
RAW_BASE="https://raw.githubusercontent.com/Z1rconium/auto_download_from_drive/main"

echo "================================================="
echo " auto_download_from_drive 一键安装脚本"
echo "================================================="
echo "安装目录: $INSTALL_DIR"
echo ""

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: 缺少依赖命令: $1"
        exit 1
    fi
}

dl() {
    local src="$1" dst="$2"
    echo "  -> $src"
    wget -q -O "$dst" "$RAW_BASE/$src" || {
        echo "ERROR: 下载失败: $src"
        exit 1
    }
}

require_cmd wget
require_cmd python3
require_cmd systemctl

echo "[1/5] 清理旧安装..."
systemctl stop sync.service 2>/dev/null || true
systemctl disable sync.service 2>/dev/null || true
rm -f /etc/systemd/system/sync.service
systemctl daemon-reload
systemctl reset-failed sync.service 2>/dev/null || true
rm -rf "$INSTALL_DIR"

echo "[2/5] 创建目录..."
mkdir -p "$INSTALL_DIR"

echo "[3/5] 下载核心文件..."
dl "sync_daemon.py" "$INSTALL_DIR/sync_daemon.py"

echo "[4/5] 创建默认配置..."
cat > "$INSTALL_DIR/config.json" << 'JSONEOF'
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 1,
  "max_retry_count": 5,
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
      "dest_path": "/path/to/local/download/folder",
      "enabled": false,
      "_comment": "source_path 支持本地挂载目录 /mnt/pikpak/My Pack，也支持 rclone remote 写法 pikpak:My Pack；确认路径正确后再改为 true"
    }
  ]
}
JSONEOF

chown root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"
chown root:root "$INSTALL_DIR/sync_daemon.py" "$INSTALL_DIR/config.json"
chmod 644 "$INSTALL_DIR/sync_daemon.py" "$INSTALL_DIR/config.json"

echo "[5/5] 写入并启动 systemd 服务..."
cat > /etc/systemd/system/sync.service << SVCEOF
[Unit]
Description=Rclone Auto Sync Daemon
After=network.target
Wants=network-online.target

[Service]
Type=notify
NotifyAccess=main
User=root
Group=root
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/sync_daemon.py
Restart=always
RestartSec=10
WatchdogSec=60
StartLimitIntervalSec=300
StartLimitBurst=5
KillSignal=SIGTERM
TimeoutStartSec=600
TimeoutStopSec=360
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable sync.service
systemctl restart sync.service

echo ""
echo "================================================="
echo " 安装完成"
echo "================================================="
echo "服务状态:"
systemctl is-active sync.service && echo "  [OK] sync.service 运行中" || echo "  [!!] sync.service 未运行"
echo ""
echo "接下来改这个文件:"
echo "  $INSTALL_DIR/config.json"
echo ""
echo "常用命令:"
echo "  sudo systemctl restart sync.service"
echo "  sudo systemctl status sync.service"
echo "  sudo journalctl -u sync.service -f"
