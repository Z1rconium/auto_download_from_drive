#!/bin/bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 请使用 root 权限运行: sudo ./update.sh"
    exit 1
fi

INSTALL_DIR="/opt/sync"
RAW_BASE="https://raw.githubusercontent.com/Z1rconium/auto_download_from_drive/main"
TMP_DIR="$(mktemp -d)"
WAS_ACTIVE=0
NEW_DAEMON="$TMP_DIR/sync_daemon.py"
NEW_SERVICE="$TMP_DIR/sync.service"
DAEMON_BACKUP="$TMP_DIR/sync_daemon.py.bak"
SERVICE_BACKUP="$TMP_DIR/sync.service.bak"
DAEMON_INSTALLED=0
SERVICE_INSTALLED=0
HAD_SERVICE_FILE=0

on_exit() {
    local exit_code="$1"
    if [ "$exit_code" -ne 0 ]; then
        echo ""
        echo "更新失败，尝试回滚已覆盖文件 ..."

        if [ "$DAEMON_INSTALLED" -eq 1 ] && [ -f "$DAEMON_BACKUP" ]; then
            install -D -m 644 "$DAEMON_BACKUP" "$INSTALL_DIR/sync_daemon.py" 2>/dev/null || true
            chown root:root "$INSTALL_DIR/sync_daemon.py" 2>/dev/null || true
            echo "  -> 已恢复旧 sync_daemon.py"
        fi

        if [ "$SERVICE_INSTALLED" -eq 1 ]; then
            if [ "$HAD_SERVICE_FILE" -eq 1 ] && [ -f "$SERVICE_BACKUP" ]; then
                install -D -m 644 "$SERVICE_BACKUP" /etc/systemd/system/sync.service 2>/dev/null || true
                echo "  -> 已恢复旧 sync.service"
            else
                rm -f /etc/systemd/system/sync.service 2>/dev/null || true
                echo "  -> 已移除新 sync.service"
            fi
            systemctl daemon-reload 2>/dev/null || true
        fi

        if [ "$WAS_ACTIVE" -eq 1 ]; then
            echo "更新失败，尝试恢复 sync.service ..."
            systemctl start sync.service 2>/dev/null || true
        fi
    fi
    rm -rf "$TMP_DIR"
    exit "$exit_code"
}
trap 'on_exit $?' EXIT

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: 缺少依赖命令: $1"
        exit 1
    fi
}

dl() {
    local src="$1" dst="$2"
    echo "  -> 下载 $src"
    wget -q -O "$dst" "$RAW_BASE/$src" || {
        echo "ERROR: 下载失败: $src"
        exit 1
    }
}

require_cmd wget
require_cmd python3
require_cmd systemctl

if [ ! -d "$INSTALL_DIR" ] || [ ! -f "$INSTALL_DIR/sync_daemon.py" ]; then
    echo "ERROR: 未检测到安装目录 $INSTALL_DIR，请先执行 sudo ./start.sh"
    exit 1
fi

echo "================================================="
echo " auto_download_from_drive 一键更新脚本"
echo "================================================="
echo "更新目录: $INSTALL_DIR"
echo "保留配置: $INSTALL_DIR/config.json"
echo ""

echo "[1/5] 下载最新核心文件..."
dl "sync_daemon.py" "$NEW_DAEMON"

cat > "$NEW_SERVICE" << SVCEOF
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

echo "[2/5] 验证新文件并备份当前文件..."
python3 -m py_compile "$NEW_DAEMON"
cp -p "$INSTALL_DIR/sync_daemon.py" "$DAEMON_BACKUP"
if [ -f /etc/systemd/system/sync.service ]; then
    cp -p /etc/systemd/system/sync.service "$SERVICE_BACKUP"
    HAD_SERVICE_FILE=1
fi
echo "  -> 新 sync_daemon.py 编译通过"
echo "  -> 当前文件已备份"

echo "[3/5] 停止服务..."
if systemctl is-active --quiet sync.service; then
    WAS_ACTIVE=1
    systemctl stop sync.service
    echo "  -> 已停止 sync.service"
else
    echo "  -> sync.service 当前未运行"
fi

echo "[4/5] 安装新文件并修正权限..."
chown root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"

DAEMON_INSTALLED=1
install -D -m 644 "$NEW_DAEMON" "$INSTALL_DIR/sync_daemon.py"
chown root:root "$INSTALL_DIR/sync_daemon.py"
chmod 644 "$INSTALL_DIR/sync_daemon.py"

SERVICE_INSTALLED=1
install -D -m 644 "$NEW_SERVICE" /etc/systemd/system/sync.service
chown root:root /etc/systemd/system/sync.service

echo "[5/5] 刷新并启动服务..."
systemctl daemon-reload
systemctl enable sync.service >/dev/null 2>&1 || true
if [ "$WAS_ACTIVE" -eq 1 ]; then
    systemctl restart sync.service
    echo "  -> 已重启 sync.service"
else
    echo "  -> sync.service 原先未运行，未自动启动"
fi

echo ""
echo "================================================="
echo " 更新完成"
echo "================================================="
echo "已保留配置文件:"
echo "  - $INSTALL_DIR/config.json"
echo ""
echo "服务状态:"
if systemctl is-active --quiet sync.service; then
    echo "  [OK] sync.service 运行中"
else
    echo "  [!!] sync.service 未运行"
fi
