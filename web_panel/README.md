# Sync Daemon Web 管理面板

一个基于 Flask + Socket.IO 的轻量级 Web 管理面板，用于可视化管理和监控后端同步守护进程（Sync Daemon）及其 rclone 传输进度。  
后端使用 Gunicorn + gevent-websocket 在生产环境中运行，所有接口通过 API Key 保护，仅监听本机 `127.0.0.1:5000`，需通过 Nginx 反向代理对外提供 HTTPS 访问。

---

## 0. 目录与数据文件约定

本仓库包含的是“Web 管理面板”本身，不包含实际执行同步任务的 Sync Daemon。  
面板默认依赖上游守护进程在上级目录写入以下文件：

- `../config.json`：同步配置，Web 面板读写。
- `../sync_state.json`：文件状态统计数据（基础状态、待同步、已同步、失败等），由后台同步程序维护。
- `../sync.log`：同步日志文件（或轮转后的 `sync.log.1`）。
- `../active_transfers.json`：当前活跃 rclone 任务列表，由后台进程维护；面板基于此调用 rclone RC 接口获取实时进度。

如果需要自定义路径，可以修改 `app.py` 中的：

```python
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
STATE_PATH = os.path.join(BASE_DIR, 'sync_state.json')
LOG_PATH = os.path.join(BASE_DIR, 'sync.log')
TRANSFERS_PATH = os.path.join(BASE_DIR, 'active_transfers.json')
```

---

## 1. 使用教程（Debian / Ubuntu）

### 1.1 系统要求

- 操作系统：Debian / Ubuntu（或其他 systemd + apt 体系的 Linux）
- 运行环境：
  - Python 3.8+（含 `python3-venv`、`python3-pip`）
  - systemd
  - rclone（开启 RC 接口，用于实时进度）
  - Nginx（反向代理与 HTTPS）

依赖包示例安装：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

### 1.2 部署目录与代码放置

默认的 `start.sh` 假定面板代码部署在：

```bash
/root/sync/web_panel
```

该路径被写死在 `start.sh` 中：

```bash
APP_DIR="/root/sync/web_panel"
```

推荐做法：

1. 在服务器上创建目录并放置代码：
   ```bash
   sudo mkdir -p /root/sync
   cd /root/sync
   sudo git clone <your-repo-url> web_panel
   cd /root/sync/web_panel
   ```
2. 如需放在其他目录（例如 `/opt/web_panel`），请同步修改 `start.sh` 中的 `APP_DIR`，并确保该目录下存在本项目代码。

### 1.3 创建虚拟环境与安装依赖（可选手动 / 将由脚本自动处理）

项目依赖在 `requirements.txt` 中，包含：

- Flask / Flask-SocketIO / Flask-CORS
- requests
- gunicorn
- gevent / gevent-websocket
- python-dotenv

`start.sh` 首次运行时会自动：

- 在当前目录创建 `venv/`
- 安装 `requirements.txt`
- 标记 `venv/installed`

如果你想手动预安装，可以执行：

```bash
cd /root/sync/web_panel
python3 -m venv venv
./venv/bin/python3 -m pip install -r requirements.txt
```

### 1.4 配置环境变量（.env）

系统级配置通过环境变量注入，service 会从 `$APP_DIR/.env` 读取。  
在 `/root/sync/web_panel` 下创建 `.env` 文件：

```bash
cd /root/sync/web_panel
nano .env
```

示例内容：

```env
WEB_PANEL_API_KEY=请替换为强随机字符串
WEB_PANEL_ALLOWED_ORIGINS=https://your-domain.com
WEB_PANEL_LOG_LEVEL=INFO
WEB_PANEL_SECRET_KEY=用于签发 session 的随机字符串
WEB_PANEL_SESSION_TTL_SECONDS=1800
```

说明：

- `WEB_PANEL_API_KEY`  
  所有 `/api/*` 请求都需要通过 `X-API-Key` 头携带此值进行认证。若不设置该变量，所有接口将无鉴权开放，仅建议在本机开发环境短暂使用。

- `WEB_PANEL_ALLOWED_ORIGINS`  
  逗号分隔的前端域名列表，例如：
  `https://your-domain.com,https://sub.your-domain.com`。  
  仅这些 Origin 可以访问 `/api/*` 和 Socket.IO。

- `WEB_PANEL_SECRET_KEY` / `WEB_PANEL_SESSION_TTL_SECONDS`  
  用于 Flask session 加密及登录状态有效期（滑动过期），成功认证一次后会在浏览器内通过 HttpOnly + Secure Cookie 维持登陆。

建议限制权限：

```bash
chmod 600 .env
```

### 1.5 一键安装 & 以 systemd 后台运行

确保当前目录在 `/root/sync/web_panel`，并以 root 身份执行：

```bash
cd /root/sync/web_panel
sudo ./start.sh
```

脚本会自动完成：

1. 检查当前用户是否为 root（否则退出）。
2. 创建并初始化虚拟环境 `venv/`。
3. 在 `/var/log/web-panel` 下创建日志目录。
4. 生成 `/etc/systemd/system/web-panel.service`：
   - `WorkingDirectory=/root/sync/web_panel`
   - 使用 gunicorn + gevent-websocket 运行 `app:app`
   - 监听地址 `127.0.0.1:5000`
   - 将 `access.log` / `error.log` 写入 `/var/log/web-panel/`
   - `After=network.target sync.service`，`Wants=sync.service`（依赖你的同步守护服务）
5. 运行：
   ```bash
   systemctl daemon-reload
   systemctl enable web-panel.service
   systemctl restart web-panel.service
   ```

检查服务状态：

```bash
systemctl status web-panel.service
```

若正常，说明 Web 管理面板已在后台以生产模式运行。

### 1.6 配置 Nginx 反向代理（含 WebSocket）

管理面板只监听本机 `127.0.0.1:5000`，需通过 Nginx 暴露到外部。  
示例 server 配置（请替换你的域名与证书路径）：

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # 静态页面 + 普通 HTTP API
    location / {
        proxy_pass http://127.0.0.1:5000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Socket.IO / WebSocket
    location /socket.io {
        proxy_pass http://127.0.0.1:5000/socket.io;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
    }
}
```

配置完成后重载 Nginx：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

确保 Nginx 暴露的域名与 `.env` 中 `WEB_PANEL_ALLOWED_ORIGINS` 保持一致。

### 1.7 常用运维命令

```bash
# 查看服务状态
systemctl status web-panel.service

# 重启服务（例如修改了 .env 或代码后）
systemctl restart web-panel.service

# 停止 / 禁用服务
systemctl stop web-panel.service
systemctl disable web-panel.service

# 查看 Gunicorn 错误日志
journalctl -u web-panel.service -e
tail -f /var/log/web-panel/error.log
```

### 1.8 Web 界面使用说明

启动后，通过浏览器访问：

- `https://your-domain.com/`

首次访问某些接口可能会提示输入 API Key：

- 前端在每次请求 `/api/*` 时，如果收到 `401`，会弹出对话框要求输入 API Key。
- 输入正确后本次请求会携带 `X-API-Key` 头；后端验证通过后会颁发带有效期的 HttpOnly session cookie，后续一段时间内无需重复输入。

界面包含四个主功能页签：

1. `配置管理`
2. `状态监控`
3. `实时进度`
4. `日志查看`

详细功能说明见下文“实现的功能”章节。

---

## 2. 实现的功能（分点）

### 2.1 配置管理（/api/config, /api/config/rules）

后端文件：`app.py` + 前端模板 `templates/index.html`。

功能：

- 读取并展示 `config.json` 的核心字段：
  - `scan_interval_seconds`：扫描间隔（秒）。
  - `rclone_refresh_interval_seconds`：rclone 进度刷新间隔（秒）。
  - `max_concurrent_downloads`：最大并发下载数。
  - `max_retry_count`：单文件最大重试次数。
  - `bandwidth_limit_mbps`：带宽限制（MB/s，0 表示不限）。
  - `rclone_command`：执行 rclone 的命令模板。
  - `rclone_service_name`：rclone 对应的 systemd 服务名，必须以 `rclone-` 开头。
  - `sync_service_name`：同步守护脚本的 systemd 服务名，固定写死为 `sync.service`，前端只读。

- Web 表单编辑这些配置并保存到 `config.json`，同时做基础合法性校验：
  - 间隔 > 0、并发数 ≥ 1、重试次数 ≥ 0、带宽限制 ≥ 0。
  - `rclone_service_name` 必须以 `rclone-` 开头，否则拒绝保存并返回错误。
  - `sync_service_name` 不接受用户输入，后端保存时会强制覆盖为 `sync.service`。

- 同步规则（rules）管理：
  - 通过“添加规则”按钮弹窗输入：
    - `source_path`：源路径。
    - `dest_path`：目标路径。
  - 后端在 `/api/config/rules` 中将新规则附加到 `config.json` 的 `rules` 数组，并默认 `enabled = True`。
  - 可对现有规则：
    - 修改源/目标路径。
    - 开关 `enabled` 状态。
    - 删除规则（`DELETE /api/config/rules/<index>`）。

- 保存配置时，后端会自动尝试：
  ```bash
  systemctl restart sync.service
  ```
  并在返回信息中附上重启是否成功的提示。

### 2.2 状态监控（/api/state, /api/stats）

后端文件：`app.py`；数据文件：`sync_state.json`。

功能：

- `/api/state`：直接返回 `sync_state.json` 的完整内容；若文件不存在则返回空结构。
- `/api/stats`：在后端遍历 `sync_state.json` 中的所有规则与文件，聚合出：
  - `total`：总文件数。
  - `baseline`：基线文件数。
  - `pending`：待同步。
  - `synced`：已同步。
  - `failed`：失败。
  - `permanent_failed`：永久失败。

前端行为：

- 使用卡片形式展示上述汇总统计。
- 提供文件列表表格：
  - 支持按“规则（源路径）”过滤。
  - 内部分页展示（默认每页 30 条）。
  - 显示：
    - 文件路径
    - 当前状态（彩色标签）
    - 重试次数 (`retry_count`)
    - 最后错误信息 (`last_error`)

### 2.3 实时进度监控（/api/progress + WebSocket）

后端文件：`app.py`, `rclone_monitor.py`；数据文件：`active_transfers.json`。

数据来源：

- `active_transfers.json` 由后台同步守护进程实时维护，结构为：
  - key：任务 ID
  - value：包含 `rc_port`、`source_file` 等信息

核心逻辑：

- `rclone_monitor.get_all_transfers_progress(transfers_path)`：
  - 读取 `active_transfers.json`。
  - 对每条任务，取出 `rc_port`，向 `http://127.0.0.1:<rc_port>/core/stats` 发送 HTTP POST。
  - 优先使用 `transferring[0]` 中的字段获得当前文件级别的进度信息：
    - 当前文件名 `name`
    - 百分比 `percentage`
    - 当前文件已传输字节数 `bytes` 和总大小 `size`
    - ETA `eta`
    - 速度：使用全局 `speed` 作为瞬时速度
  - 构建统一的进度结构：
    - `bytes`、`totalBytes`
    - `speed`、`speedMBps`
    - `eta`（秒）
    - `percentage`
    - `current_file`
  - 若 rclone RC 暂不可用（服务未就绪或任务刚启动），则返回一个 `connecting` 状态的占位结果。

- `/api/progress`：
  - 直接调用 `get_all_transfers_progress` 并返回聚合结果。

- Socket.IO 实时推送：
  - 启动时在后端创建一个后台任务 `background_progress_monitor`：
    - 每秒读取一次 progress 数据。
    - 向所有连接的前端广播事件：`progress_update`。
  - 前端订阅 `socket.on('progress_update', renderProgress)`，实时刷新界面。

前端展示：

- “实时进度”页签下：
  - 将每个活跃任务渲染为一个卡片。
  - 支持“连接中”骨架动画（当 rclone RC 尚未准备好）。
  - 显示：
    - 当前文件名
    - 进度条（至少 5% 宽度以保证可见）
    - 百分比、速度（MB/s）、剩余时间（秒）
    - 已传输 / 总大小（MB）

### 2.4 日志查看（/api/logs）

后端文件：`app.py`；数据文件：`sync.log` 或 `sync.log.1`。

功能：

- `/api/logs` 接受 `lines` 查询参数（默认 100，前端传 200）。
- 若存在 `sync.log`，则读取尾部指定行；否则回退到 `sync.log.1`。
- 返回 JSON 结构：
  ```json
  { "logs": ["...每行一条..."] }
  ```

前端展示：

- “日志查看”页签中：
  - 点击“刷新日志”按钮调用 `/api/logs`。
  - 将日志逐行渲染在一个固定高度、可滚动的终端风格区域中。

### 2.5 安全机制与 API 鉴权

后端文件：`app.py`。

机制：

- API Key 认证（装饰器 `@require_api_key` 保护所有 `/api/*`）：
  - 若未设置 `WEB_PANEL_API_KEY`：
    - 记录警告日志，直接放行（开发模式）。
  - 若设置了 `WEB_PANEL_API_KEY`：
    - 优先检查 session 中是否存在未过期的 `api_auth_until`。
    - 若无有效 session，则要求请求头携带：
      ```http
      X-API-Key: <WEB_PANEL_API_KEY>
      ```
      并使用 `hmac.compare_digest` 做时间常量级对比。
    - 验证通过后写入新的过期时间，实现“滑动过期”。

- Session & Cookie 安全：
  - `SESSION_COOKIE_HTTPONLY = True`
  - `SESSION_COOKIE_SECURE = True`（仅 HTTPS）
  - `SESSION_COOKIE_SAMESITE = 'Strict'`

- CORS 限制：
  - 仅允许 `.env` 中 `WEB_PANEL_ALLOWED_ORIGINS` 列出的 Origin 调用 `/api/*`。
  - Socket.IO 也使用同一组允许的来源。

### 2.6 前端交互与体验细节

前端文件：`templates/index.html`，基于 Tailwind CSS + 浏览器原生 JS。

主要行为：

- 使用 `fetch` 封装为 `apiFetch`：
  - 若返回 `401`，弹框要求输入 API Key，再次携带该 Key 重试。
- Tab 切换逻辑 `showTab(tab)`：
  - `config`：加载配置。
  - `status`：加载状态和统计。
  - `progress`：开启 1 秒轮询 `/api/progress`，并同时依赖 WebSocket 推送。
  - `logs`：加载日志。
- “状态监控”表格分页、过滤、彩色标签等 UI。
- “实时进度”中的条形进度动画与骨架加载效果。

---

## 3. 基本架构

### 3.1 整体拓扑

从外到内的请求流向：

```text
浏览器 (HTTPS)
    |
    v
Nginx 反向代理 (SSL 终止, WebSocket 升级)
    |
    v
Gunicorn + gevent-websocket (web-panel.service)
    |
    v
Flask 应用 app.py
    |
    +--> 读写 config.json / sync_state.json / sync.log / active_transfers.json
    |
    +--> 通过 rclone_monitor.py 调用 rclone RC (/core/stats)
```

同步数据流：

- Sync Daemon / rclone：
  - 扫描源目录、执行下载上传。
  - 维护 `sync_state.json`、`active_transfers.json`、`sync.log`。
- Web 管理面板：
  - 仅通过文件与 rclone RC 接口观察和控制（通过配置与 systemd 重启），不直接执行数据同步逻辑。

### 3.2 后端模块划分

1. `app.py`
   - 创建 Flask 应用与 Socket.IO 实例。
   - 通过环境变量完成：
     - API Key 与 session 安全配置。
     - CORS 允许源配置。
   - 提供 REST API：
     - `/api/config`（GET / POST）
     - `/api/config/rules`（POST 新规则）
     - `/api/config/rules/<int:rule_index>`（DELETE 删除规则）
     - `/api/state`
     - `/api/stats`
     - `/api/logs`
     - `/api/transfers`（原始转储 `active_transfers.json`）
     - `/api/progress`（通过 rclone_monitor 聚合实时进度）
   - Socket.IO：
     - 事件：`progress_update`，由后台任务每秒广播当前进度。
     - 事件钩子：`connect` / `disconnect`。
   - 运行模式：
     - 作为脚本直接运行时：使用 `socketio.run(app, host='127.0.0.1', port=5000)`。
     - 生产环境中：由 Gunicorn 载入 `app:app`，使用 gevent-websocket worker。

2. `rclone_monitor.py`
   - 封装 rclone RC 接口调用逻辑：
     - `get_rclone_progress(rc_port)`：向 `http://127.0.0.1:<rc_port>/core/stats` 发 POST 请求，并从返回 JSON 中提取当前文件进度。
     - `get_all_transfers_progress(transfers_path)`：遍历 `active_transfers.json` 中的所有任务，按任务 ID 聚合当前进度信息。

3. `start.sh`
   - 需以 root 权限执行：
     - 检查 / 创建 Python 虚拟环境并安装依赖。
     - 创建 `/var/log/web-panel` 日志目录。
     - 生成 `/etc/systemd/system/web-panel.service`：
       - `ExecStart` 使用：
         ```bash
         gunicorn --workers 1 \
                  --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
                  --bind 127.0.0.1:5000 \
                  ...
                  app:app
         ```
       - 限制 worker 数量为 1，以避免 WebSocket 连接跨 worker 导致状态不一致。
     - 自动启用并重启服务。

4. `templates/index.html`
   - 单页 Web UI：
     - 使用 Tailwind CDN 进行快速样式构建。
     - 通过 Socket.IO CDN 建立实时连接。
     - 内嵌所有前端逻辑（无独立单页构建工具）。

### 3.3 运行时行为与数据一致性

- WebSocket & worker 模式：
  - 通过 gevent-websocket worker 提供长连接支持。
  - 限制为单 worker，避免同一客户端被不同进程处理导致进度推送不一致。

- Session 设计：
  - 使用短期 session（默认 1800 秒），并在每次请求时“滑动续期”，减轻频繁输入 API Key 的负担。
  - Session 存储在服务端（Flask 默认机制），前端仅持有不可读的 HttpOnly Cookie。

- 与上游 Sync Daemon 的耦合：
  - Web 面板只读写少量控制面文件（主要是 `config.json`），其余状态均为只读。
  - 同步规则变更后，通过 `systemctl restart <sync_service_name>` 驱动上游重新加载配置。

---

如需在此基础上扩展更多功能（例如：手动触发单文件重试、暂停/恢复任务、用户系统等），可以在 `app.py` 中新增 API，并在 `templates/index.html` 中增加对应的交互与展示逻辑。当前版本的核心目标是：在不侵入现有同步逻辑的前提下，提供安全、可视化、可观测的管理入口。
