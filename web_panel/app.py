from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, jsonify, request, session
from flask_socketio import SocketIO
from flask_cors import CORS
import json
import os
import threading
import subprocess
import time
import logging
import hmac
from functools import wraps
from datetime import datetime
from rclone_monitor import get_all_transfers_progress

# ==================== Configuration ====================
app = Flask(__name__)

# Load environment variables for authentication and CORS
API_KEY = os.getenv('WEB_PANEL_API_KEY', '')
ALLOWED_ORIGINS = os.getenv('WEB_PANEL_ALLOWED_ORIGINS', 'http://localhost,https://localhost')
LOG_LEVEL = os.getenv('WEB_PANEL_LOG_LEVEL', 'INFO').upper()
SESSION_TTL_SECONDS = int(os.getenv('WEB_PANEL_SESSION_TTL_SECONDS', '1800'))
SECRET_KEY = os.getenv('WEB_PANEL_SECRET_KEY', '')
if not SECRET_KEY:
    SECRET_KEY = os.urandom(32).hex()

app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('web_panel')

# Configure CORS with restricted origins
cors_origins = [o.strip() for o in ALLOWED_ORIGINS.split(',')]
CORS(app, resources={r"/api/*": {"origins": cors_origins}})
socketio = SocketIO(app, cors_allowed_origins=cors_origins)

# ==================== Authentication ====================
def _has_valid_session():
    expires_at = session.get('api_auth_until', 0)
    return isinstance(expires_at, int) and expires_at > int(time.time())


def require_api_key(f):
    """Decorator to require API Key authentication for protected routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not API_KEY:
            # API Key not configured - allow access (development mode)
            logger.warning('API_KEY not set - authentication disabled (development mode)')
            return f(*args, **kwargs)
        
        if _has_valid_session():
            # Sliding expiration to reduce frequent re-auth prompts.
            session['api_auth_until'] = int(time.time()) + SESSION_TTL_SECONDS
            return f(*args, **kwargs)

        api_key = request.headers.get('X-API-Key', '')
        if not api_key or not hmac.compare_digest(api_key, API_KEY):
            logger.warning(f'Unauthorized API access attempt from {request.remote_addr}')
            return jsonify({'error': 'Unauthorized - Invalid or missing API key'}), 401

        # Promote a valid API key auth to short-lived HttpOnly session.
        session['api_auth_until'] = int(time.time()) + SESSION_TTL_SECONDS
        logger.info(f'API request from {request.remote_addr}: {request.method} {request.path}')
        return f(*args, **kwargs)
    
    return decorated_function

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
STATE_PATH = os.path.join(BASE_DIR, 'sync_state.json')
LOG_PATH = os.path.join(BASE_DIR, 'sync.log')
TRANSFERS_PATH = os.path.join(BASE_DIR, 'active_transfers.json')


def _run_systemctl(action, service_name, timeout=15):
    command = ['systemctl', action, service_name]

    if os.geteuid() == 0:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        err = (result.stderr or result.stdout or '').strip()
        return result.returncode == 0, err

    errors = []

    # Prefer sudoers-based NOPASSWD path for service operations.
    try:
        sudo_result = subprocess.run(
            ['sudo', '-n'] + command,
            capture_output=True, text=True, timeout=timeout
        )
        if sudo_result.returncode == 0:
            return True, ''
        sudo_err = (sudo_result.stderr or sudo_result.stdout or '').strip()
        if sudo_err:
            errors.append(sudo_err)
    except Exception as e:
        errors.append(str(e))

    # Fallback to direct systemctl (for environments that rely on polkit rules).
    try:
        direct_result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if direct_result.returncode == 0:
            return True, ''
        direct_err = (direct_result.stderr or direct_result.stdout or '').strip()
        if direct_err:
            errors.append(direct_err)
    except Exception as e:
        errors.append(str(e))

    deduped = []
    for msg in errors:
        if msg and msg not in deduped:
            deduped.append(msg)
    return False, ' | '.join(deduped)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET'])
@require_api_key
def get_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    try:
        new_config = request.json
        if new_config.get('scan_interval_seconds', 0) <= 0:
            return jsonify({'error': 'scan_interval_seconds must be > 0'}), 400
        if new_config.get('rclone_refresh_interval_seconds', 0) <= 0:
            return jsonify({'error': 'rclone_refresh_interval_seconds must be > 0'}), 400
        if new_config.get('max_concurrent_downloads', 0) < 1:
            return jsonify({'error': 'max_concurrent_downloads must be >= 1'}), 400
        if new_config.get('max_retry_count', -1) < 0:
            return jsonify({'error': 'max_retry_count must be >= 0'}), 400
        if new_config.get('bandwidth_limit_mbps', -1) < 0:
            return jsonify({'error': 'bandwidth_limit_mbps must be >= 0'}), 400

        with open(CONFIG_PATH, 'w') as f:
            json.dump(new_config, f, indent=2)

        # 如果配置了同步脚本 service 名，自动 restart
        sync_service = new_config.get('sync_service_name', '').strip()
        restart_msg = ''
        if sync_service:
            try:
                ok, err = _run_systemctl('restart', sync_service, timeout=15)
                if ok:
                    restart_msg = f'，已自动重启 {sync_service}'
                else:
                    restart_msg = f'，但重启 {sync_service} 失败：{err or "unknown error"}'
            except Exception as e:
                restart_msg = f'，但重启 {sync_service} 出错：{e}'

        return jsonify({'success': True, 'message': f'配置已保存{restart_msg}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/rules', methods=['POST'])
@require_api_key
def add_config_rule():
    try:
        payload = request.json or {}
        source_path = (payload.get('source_path') or '').strip()
        dest_path = (payload.get('dest_path') or '').strip()

        if not source_path:
            return jsonify({'error': 'source_path is required'}), 400
        if not dest_path:
            return jsonify({'error': 'dest_path is required'}), 400

        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)

        if 'rules' not in config:
            config['rules'] = []
        if not isinstance(config['rules'], list):
            return jsonify({'error': 'config.rules must be an array'}), 400

        new_rule = {
            'source_path': source_path,
            'dest_path': dest_path,
            'enabled': True,
            '_comment': 'Set enabled=true after validating source and destination paths.'
        }
        config['rules'].append(new_rule)

        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

        return jsonify({'success': True, 'message': '规则已添加', 'rule': new_rule})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/rules/<int:rule_index>', methods=['DELETE'])
@require_api_key
def delete_config_rule(rule_index):
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)

        rules = config.get('rules', [])
        if not isinstance(rules, list):
            return jsonify({'error': 'config.rules must be an array'}), 400
        if rule_index < 0 or rule_index >= len(rules):
            return jsonify({'error': 'rule index out of range'}), 400

        deleted_rule = rules.pop(rule_index)
        config['rules'] = rules

        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

        return jsonify({'success': True, 'message': '规则已删除', 'rule': deleted_rule})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/state', methods=['GET'])
@require_api_key
def get_state():
    try:
        if not os.path.exists(STATE_PATH):
            return jsonify({'rules': {}})
        with open(STATE_PATH, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
@require_api_key
def get_stats():
    try:
        if not os.path.exists(STATE_PATH):
            return jsonify({'total': 0, 'baseline': 0, 'pending': 0, 'synced': 0, 'failed': 0, 'permanent_failed': 0})

        with open(STATE_PATH, 'r') as f:
            state = json.load(f)

        stats = {'total': 0, 'baseline': 0, 'pending': 0, 'synced': 0, 'failed': 0, 'permanent_failed': 0}
        for rule_id, rule_data in state.get('rules', {}).items():
            for file_path, file_info in rule_data.get('files', {}).items():
                stats['total'] += 1
                status = file_info.get('status', 'unknown')
                if status in stats:
                    stats[status] += 1

        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
@require_api_key
def get_logs():
    try:
        lines = int(request.args.get('lines', 100))
        log_file = LOG_PATH if os.path.exists(LOG_PATH) else LOG_PATH + '.1'

        if not os.path.exists(log_file):
            return jsonify({'logs': []})

        with open(log_file, 'r') as f:
            all_lines = f.readlines()
            return jsonify({'logs': all_lines[-lines:]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transfers', methods=['GET'])
@require_api_key
def get_transfers():
    try:
        if not os.path.exists(TRANSFERS_PATH):
            return jsonify({'transfers': {}})
        with open(TRANSFERS_PATH, 'r') as f:
            return jsonify({'transfers': json.load(f)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress', methods=['GET'])
@require_api_key
def get_progress():
    try:
        return jsonify(get_all_transfers_progress(TRANSFERS_PATH))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def background_progress_monitor():
    while True:
        try:
            progress_data = get_all_transfers_progress(TRANSFERS_PATH)
            socketio.emit('progress_update', progress_data)
        except Exception as e:
            print(f"Progress monitor error: {e}")
        socketio.sleep(1)

# Start background task globally (runs when Gunicorn imports app)
socketio.start_background_task(background_progress_monitor)

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    
    # Log startup information
    if API_KEY:
        logger.info('Web panel starting with API authentication enabled')
    else:
        logger.warning('Web panel starting WITHOUT API authentication (development mode)')
    logger.info(f'Listening on 127.0.0.1:5000 (local only)')
    logger.info(f'Allowed CORS origins: {", ".join(cors_origins)}')
    
    # Use eventlet worker for socketio, bind to localhost only
    socketio.run(app, host='127.0.0.1', port=5000, debug=False)
