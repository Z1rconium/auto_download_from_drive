#!/bin/bash
set -euo pipefail

# ============================================================
#  auto_download_from_drive 一键安装脚本
#  Repo: https://github.com/Z1rconium/auto_download_from_drive
# ============================================================

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 请使用 root 权限运行: sudo ./start.sh"
    exit 1
fi

INSTALL_DIR="/opt/sync"
WEB_DIR="$INSTALL_DIR/web_panel"
RAW_BASE="https://raw.githubusercontent.com/Z1rconium/auto_download_from_drive/main"
SERVICE_USER="web-panel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "================================================="
echo " auto_download_from_drive 一键安装脚本"
echo "================================================="
echo "安装目录: $INSTALL_DIR"
echo ""

# ----------------------------------------------------------------
# 1. 清理旧残留
# ----------------------------------------------------------------
echo "[1/8] 清理上一次残留..."

for svc in web-panel.service sync.service; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done

rm -f /etc/systemd/system/web-panel.service
rm -f /etc/systemd/system/sync.service
rm -f /etc/sudoers.d/web-panel
rm -f /etc/polkit-1/rules.d/10-web-panel.rules

systemctl daemon-reload
systemctl reset-failed web-panel.service sync.service 2>/dev/null || true

rm -rf "$INSTALL_DIR"
rm -rf "$REPO_ROOT/sync"
rm -f "$REPO_ROOT/.env"
rm -f "$REPO_ROOT"/*.log
rm -rf /var/log/web-panel

if id "$SERVICE_USER" &>/dev/null; then
    pkill -u "$SERVICE_USER" 2>/dev/null || true
    userdel --force "$SERVICE_USER" 2>/dev/null || true
fi
if getent group "$SERVICE_USER" >/dev/null; then
    groupdel "$SERVICE_USER" 2>/dev/null || true
fi

# ----------------------------------------------------------------
# 2. 创建目录结构
# ----------------------------------------------------------------
echo "[2/8] 创建目录结构..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$WEB_DIR/templates"
mkdir -p /var/log/web-panel

# ----------------------------------------------------------------
# 3. 下载项目文件
# ----------------------------------------------------------------
echo "[3/8] 下载项目文件..."

dl() {
    local src="$1" dst="$2"
    echo "  -> $src"
    wget -q -O "$dst" "$RAW_BASE/$src" || { echo "ERROR: 下载失败: $src"; exit 1; }
}

dl "sync_daemon.py"                 "$INSTALL_DIR/sync_daemon.py"
dl "web_panel/app.py"               "$WEB_DIR/app.py"
dl "web_panel/rclone_monitor.py"    "$WEB_DIR/rclone_monitor.py"
dl "web_panel/requirements.txt"     "$WEB_DIR/requirements.txt"
dl "web_panel/templates/index.html" "$WEB_DIR/templates/index.html"

# ----------------------------------------------------------------
# 4. 创建配置文件
# ----------------------------------------------------------------
echo "[4/8] 创建配置文件..."

if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cat > "$INSTALL_DIR/config.json" << 'JSONEOF'
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 3,
  "max_retry_count": 5,
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "CHANGEME-rclone-service",
  "rules": [
    {
      "source_path": "/mnt/CHANGEME/source",
      "dest_path": "/mnt/CHANGEME/dest",
      "enabled": false,
      "_comment": "enabled=false 直到确认路径正确后再改为 true"
    }
  ]
}
JSONEOF
    echo "  -> config.json 已创建（需要填写）"
else
    echo "  -> config.json 已存在，跳过"
fi

# 创建 .env
if [ ! -f "$WEB_DIR/.env" ]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > "$WEB_DIR/.env" << ENVEOF
# ===== Web Panel 环境配置 =====

# [必填] 访问 Web 面板所需的 API 密钥，设置一个强随机密码
WEB_PANEL_API_KEY=CHANGEME_REPLACE_WITH_STRONG_PASSWORD

# [必填] 允许跨域的来源，填写你的 Caddy 反代域名（逗号分隔）
WEB_PANEL_ALLOWED_ORIGINS=https://CHANGEME.example.com

# [已自动生成] Flask Session 密钥，无需修改
WEB_PANEL_SECRET_KEY=${SECRET_KEY}

# [可选] Session 有效期（秒），默认 30 分钟
WEB_PANEL_SESSION_TTL_SECONDS=1800

# [可选] 日志级别：DEBUG / INFO / WARNING / ERROR
WEB_PANEL_LOG_LEVEL=INFO
ENVEOF
    chmod 600 "$WEB_DIR/.env"
    echo "  -> .env 已创建（需要填写 WEB_PANEL_API_KEY 和 WEB_PANEL_ALLOWED_ORIGINS）"
else
    echo "  -> .env 已存在，跳过"
fi

# ----------------------------------------------------------------
# 5. 创建低权限用户
# ----------------------------------------------------------------
echo "[5/8] 创建低权限用户 $SERVICE_USER..."

if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  -> 用户 $SERVICE_USER 已创建"
else
    echo "  -> 用户 $SERVICE_USER 已存在，跳过"
fi
chown "$SERVICE_USER":"$SERVICE_USER" /var/log/web-panel

# ----------------------------------------------------------------
# 6. 设置权限 & Python 虚拟环境
# ----------------------------------------------------------------
echo "[6/8] 设置文件权限并安装 Python 依赖..."

# sync_daemon 文件：root 所有，web-panel 可读写 config.json
chown root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"
chown root:root "$INSTALL_DIR/sync_daemon.py"
chmod 644 "$INSTALL_DIR/sync_daemon.py"
chown root:"$SERVICE_USER" "$INSTALL_DIR/config.json"
chmod 664 "$INSTALL_DIR/config.json"

# web_panel 目录：web-panel 所有
chown -R "$SERVICE_USER":"$SERVICE_USER" "$WEB_DIR"
chmod 750 "$WEB_DIR"

# 日志目录
chown "$SERVICE_USER":"$SERVICE_USER" /var/log/web-panel
chmod 750 /var/log/web-panel
touch /var/log/web-panel/access.log /var/log/web-panel/error.log
chown "$SERVICE_USER":"$SERVICE_USER" /var/log/web-panel/access.log /var/log/web-panel/error.log
chmod 640 /var/log/web-panel/access.log /var/log/web-panel/error.log

# Python venv
if [ ! -d "$WEB_DIR/venv" ]; then
    python3 -m venv "$WEB_DIR/venv" || {
        echo "ERROR: 虚拟环境创建失败，请先安装: apt install python3-venv"
        exit 1
    }
fi

if [ ! -f "$WEB_DIR/venv/installed" ]; then
    echo "  安装 Python 依赖（可能需要数分钟）..."
    "$WEB_DIR/venv/bin/pip" install -q -r "$WEB_DIR/requirements.txt" || {
        echo "ERROR: 依赖安装失败，请清理后重试: rm -rf $WEB_DIR/venv"
        exit 1
    }
    touch "$WEB_DIR/venv/installed"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$WEB_DIR/venv"

# ----------------------------------------------------------------
# 7. Systemd 服务 / sudoers / polkit
# ----------------------------------------------------------------
echo "[7/8] 写入系统服务和权限规则..."

# --- sync.service ---
cat > /etc/systemd/system/sync.service << SVCEOF
[Unit]
Description=Rclone Auto Sync Daemon
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/sync_daemon.py
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
KillSignal=SIGTERM
TimeoutStopSec=360
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

# --- web-panel.service ---
cat > /etc/systemd/system/web-panel.service << SVCEOF
[Unit]
Description=Web Panel for Sync Daemon
After=network.target sync.service
Wants=sync.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${WEB_DIR}
EnvironmentFile=${WEB_DIR}/.env
ExecStart=${WEB_DIR}/venv/bin/gunicorn \\
    --workers 1 \\
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \\
    --bind 127.0.0.1:5000 \\
    --timeout 300 \\
    --graceful-timeout 2 \\
    --access-logfile /var/log/web-panel/access.log \\
    --error-logfile /var/log/web-panel/error.log \\
    --log-level info \\
    app:app
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

echo "  -> sync.service 和 web-panel.service 已写入"

# --- sudoers ---
cat > /etc/sudoers.d/web-panel << SUDOEOF
# web-panel 用户可管理 sync.service（兼容 /usr/bin 与 /bin 路径）
Cmnd_Alias WEB_PANEL_SYNC_CTL = /usr/bin/systemctl start sync.service, /usr/bin/systemctl stop sync.service, /usr/bin/systemctl restart sync.service, /usr/bin/systemctl status sync.service, /bin/systemctl start sync.service, /bin/systemctl stop sync.service, /bin/systemctl restart sync.service, /bin/systemctl status sync.service
${SERVICE_USER} ALL=(root) NOPASSWD: WEB_PANEL_SYNC_CTL
SUDOEOF
chmod 440 /etc/sudoers.d/web-panel
echo "  -> sudoers 规则已写入"

# --- polkit 规则（允许 app.py 的 subprocess 直接调用 systemctl 无需 sudo）---
if [ -d /etc/polkit-1/rules.d ]; then
    cat > /etc/polkit-1/rules.d/10-web-panel.rules << 'PKEOF'
// Allow web-panel user to manage sync.service
polkit.addRule(function(action, subject) {
    if (action.id === "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") === "sync.service" &&
        subject.user === "web-panel") {
        return polkit.Result.YES;
    }
});
PKEOF
    echo "  -> polkit 规则已写入"
else
    echo "  -> 警告: /etc/polkit-1/rules.d 不存在，跳过 polkit 规则"
    echo "     Web 面板的 daemon 控制功能可能无法使用，可手动测试:"
    echo "     sudo -u $SERVICE_USER systemctl restart sync.service"
fi

# ----------------------------------------------------------------
# 8. 启用并启动服务
# ----------------------------------------------------------------
echo "[8/8] 启用并启动服务..."

systemctl daemon-reload
systemctl enable sync.service
systemctl enable web-panel.service

echo "  启动 sync.service..."
systemctl restart sync.service

echo "  启动 web-panel.service..."
systemctl restart web-panel.service

# ================================================================
# 安装完成 - 输出待填写项
# ================================================================
echo ""
echo "================================================="
echo " 安装完成！"
echo "================================================="
echo ""
echo "服务状态:"
systemctl is-active sync.service       && echo "  [OK] sync.service       运行中" || echo "  [!!] sync.service       未运行"
systemctl is-active web-panel.service  && echo "  [OK] web-panel.service  运行中" || echo "  [!!] web-panel.service  未运行"
echo ""
echo "================================================================"
echo " !! 以下配置项需要手动填写，完成后重启对应服务 !!"
echo "================================================================"
echo ""
echo "1. $INSTALL_DIR/config.json  ← 核心配置"
echo "   - rclone_service_name : 你的 rclone 挂载 systemd 服务名（如 rclone-pikpak）"
echo "   - rules[].source_path : rclone 挂载的源目录路径"
echo "   - rules[].dest_path   : 下载文件的本地目标目录"
echo "   - rules[].enabled     : 路径确认无误后改为 true"
echo "   修改后执行: sudo systemctl restart sync.service"
echo ""
echo "2. $WEB_DIR/.env  ← Web 面板认证配置"
echo "   - WEB_PANEL_API_KEY          : 访问面板的密码（必须修改，当前为占位符）"
echo "   - WEB_PANEL_ALLOWED_ORIGINS  : 你的 Caddy 反代域名，如 https://panel.example.com"
echo "   修改后执行: sudo systemctl restart web-panel.service"
echo ""
echo "3. Caddy 反代配置（示例）:"
echo "   panel.example.com {"
echo "       @allowed remote_ip 你的.IP.地.址"
echo "       handle @allowed {"
echo "           reverse_proxy 127.0.0.1:5000"
echo "       }"
echo "       respond 403"
echo "   }"
echo ""
echo "快速检查日志:"
echo "  sudo journalctl -u sync.service -f"
echo "  sudo journalctl -u web-panel.service -f"
echo "  sudo tail -f /var/log/web-panel/error.log"
echo ""
