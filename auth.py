"""Local accounts, expiring sessions and setup proof. No plaintext passwords."""
import base64
import copy
import hashlib
import hmac
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

from engine import ValidationError
from secure_storage import read_json, write_json, protect_file

ROLES = {'admin', 'operator', 'viewer'}
ITERATIONS = 600000
IDLE_SECONDS = 1800
SESSION_SECONDS = 8 * 3600
SETUP_SECONDS = 1200


class AuthError(ValidationError):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def username_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', value):
        raise ValidationError('用户名需为3—64位英文字母、数字、点、短横线或下划线。')
    return value.lower()


def password_value(value):
    if not isinstance(value, str) or not 15 <= len(value) <= 128 or not value.strip():
        raise ValidationError('密码需为15—128个字符，建议使用不重复的长口令。')
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return {'salt': base64.b64encode(salt).decode(), 'hash': base64.b64encode(digest).decode(), 'iterations': ITERATIONS}


def password_matches(password, account):
    if not isinstance(password, str) or len(password) > 128:
        return False
    try:
        value = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), base64.b64decode(account['salt']), account['iterations'])
        return hmac.compare_digest(value, base64.b64decode(account['hash']))
    except (KeyError, ValueError, TypeError):
        return False


class AuthService:
    def __init__(self, runtime, clock=time.time):
        self.runtime, self.clock = Path(runtime), clock
        self.runtime.mkdir(parents=True, exist_ok=True)
        protect_file(self.runtime)
        self.path = self.runtime / 'accounts.dpapi'
        self.setup_path = self.runtime / 'setup-code.txt'
        self.lock = threading.RLock()
        self.sessions = {}
        self.state = read_json(self.path, default={'users': {}, 'attempts': {}, 'audit': []})
        self.saved_state = copy.deepcopy(self.state)
        if not isinstance(self.state, dict) or not isinstance(self.state.get('users'), dict):
            raise AuthError('账号资料损坏，已停止启动；请通过本机恢复备份。', 503)
        if not self.state['users']:
            self.ensure_setup_code()
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))

    def _save(self):
        now = self.clock()
        self.state['audit'] = [row for row in self.state.get('audit', []) if now - row['at'] <= 30 * 86400][-1000:]
        self.state['attempts'] = {k: v for k, v in self.state.get('attempts', {}).items() if now - v['at'] <= 900}
        try:
            write_json(self.path, self.state)
        except Exception:
            self.state = copy.deepcopy(self.saved_state)
            raise
        self.saved_state = copy.deepcopy(self.state)

    def _audit(self, action, username):
        self.state.setdefault('audit', []).append({'at': self.clock(), 'action': action, 'username': username})

    def _reload(self):
        # A local-owner CLI can rotate setup proof; never continue using its old hash.
        if self.path.exists():
            value = read_json(self.path)
            if not isinstance(value, dict) or not isinstance(value.get('users'), dict):
                raise AuthError('账号资料无效，访问已阻止。', 503)
            self.state = value
            self.saved_state = copy.deepcopy(value)

    def ensure_setup_code(self, force=False):
        with self.lock:
            self._reload()
            if self.state['users']:
                raise ValidationError('已存在账号，不能再次初始化管理员。')
            saved = self.state.get('setup', {})
            if not force and saved.get('expires_at', 0) > self.clock() and self.setup_path.exists():
                return self.setup_path
            code = secrets.token_urlsafe(32)
            self.state['setup'] = {'hash': hashlib.sha256(code.encode()).hexdigest(), 'expires_at': self.clock() + SETUP_SECONDS}
            # Protect the containing directory and empty file before writing the secret.
            self.setup_path.touch(exist_ok=True)
            protect_file(self.setup_path)
            self.setup_path.write_text(code, encoding='utf-8')
            self._save()
            return self.setup_path

    def _check_throttle(self, name):
        now = self.clock()
        for key, limit in ((name, 5), ('*', 25)):
            entry = self.state.get('attempts', {}).get(key, {})
            if now - entry.get('at', 0) < 300 and entry.get('count', 0) >= limit:
                raise AuthError('尝试次数过多，请5分钟后再试。', 429)

    def _failure(self, name):
        now = self.clock()
        for key in (name, '*'):
            entry = self.state.setdefault('attempts', {}).get(key, {})
            count = entry.get('count', 0) if now - entry.get('at', 0) < 300 else 0
            self.state['attempts'][key] = {'count': count + 1, 'at': now}
        self._audit('login_failed', name)
        self._save()

    def _session(self, name):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if now - value['last_seen'] <= IDLE_SECONDS and now - value['created_at'] <= SESSION_SECONDS}
        if len(self.sessions) >= 100:
            oldest = min(self.sessions, key=lambda k: self.sessions[k]['created_at'])
            del self.sessions[oldest]
        sid = secrets.token_urlsafe(48)
        account = self.state['users'][name]
        session = {'username': name, 'csrf_token': secrets.token_urlsafe(32), 'generation': account['generation'],
                   'created_at': now, 'last_seen': now}
        self.sessions[hashlib.sha256(sid.encode()).hexdigest()] = session
        return {'session_id': sid, 'user': {'username': name, 'role': account['role']}, 'csrf_token': session['csrf_token']}

    def setup(self, payload):
        with self.lock:
            self._reload()
            self._check_throttle('setup')
            if self.state['users']:
                raise AuthError('初始化已完成，请登录。', 409)
            code = payload.get('setup_code', '')
            setup = self.state.get('setup', {})
            if (not isinstance(code, str) or len(code) > 256 or setup.get('expires_at', 0) <= self.clock()
                    or not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), setup.get('hash', ''))):
                self._failure('setup')
                raise AuthError('初始化码无效或已过期，请在本机重新生成。')
            name, password = username_value(payload.get('username')), password_value(payload.get('password'))
            self.state['users'][name] = {**password_hash(password), 'role': 'admin', 'disabled': False, 'generation': 1}
            self.state.pop('setup', None)
            self._audit('admin_initialized', name)
            self._save()
            self.setup_path.unlink(missing_ok=True)
            return self._session(name)

    def login(self, payload):
        with self.lock:
            self._reload()
            try:
                name = username_value(payload.get('username'))
            except ValidationError:
                name = 'invalid-username'
            self._check_throttle(name)
            account = self.state['users'].get(name)
            valid = password_matches(payload.get('password'), account or self.dummy_hash)
            if not account or not valid or account.get('disabled'):
                self._failure(name)
                raise AuthError('用户名或密码不正确。')
            self.state.get('attempts', {}).pop(name, None)
            self._audit('login', name)
            self._save()
            return self._session(name)

    def authenticate(self, cookie_header, cookie_name):
        with self.lock:
            self._reload()
            try:
                cookies = SimpleCookie()
                cookies.load(cookie_header or '')
                raw = cookies[cookie_name].value
                if len(raw) > 256:
                    return None
                key = hashlib.sha256(raw.encode()).hexdigest()
            except Exception:
                return None
            session = self.sessions.get(key)
            if not session:
                return None
            now = self.clock()
            account = self.state['users'].get(session['username'])
            if (not account or account.get('disabled') or account['generation'] != session['generation']
                    or now - session['last_seen'] > IDLE_SECONDS or now - session['created_at'] > SESSION_SECONDS
                    or now < session['created_at']):
                self.sessions.pop(key, None)
                return None
            session['last_seen'] = now
            return {'user': {'username': session['username'], 'role': account['role']},
                    'csrf_token': session['csrf_token'], '_key': key}

    def status(self, session=None):
        return {'setup_required': not bool(self.state['users']), 'authenticated': bool(session),
                'user': session['user'] if session else None, 'csrf_token': session['csrf_token'] if session else None}

    def logout(self, session):
        with self.lock:
            self.sessions.pop(session['_key'], None)

    def change_password(self, session, payload):
        with self.lock:
            name = session['user']['username']
            self._check_throttle(name)
            account = self.state['users'][name]
            if not password_matches(payload.get('current_password'), account):
                self._failure(name)
                raise AuthError('当前密码不正确。')
            password = password_value(payload.get('new_password'))
            if password_matches(password, account):
                raise ValidationError('新密码不能与当前密码相同。')
            account.update(password_hash(password))
            account['generation'] += 1
            self._audit('password_changed', name)
            self._save()
            self.sessions = {key: value for key, value in self.sessions.items() if value['username'] != name}

    def users(self):
        with self.lock:
            return {'users': [{'username': name, 'role': account['role'], 'disabled': account.get('disabled', False)}
                             for name, account in sorted(self.state['users'].items())]}

    def manage_user(self, actor, payload):
        with self.lock:
            name = username_value(payload.get('username'))
            role, disabled = payload.get('role'), payload.get('disabled', False)
            if role not in ROLES or type(disabled) is not bool:
                raise ValidationError('账号角色或启用状态无效。')
            existing = self.state['users'].get(name)
            if not existing and len(self.state['users']) >= 100:
                raise ValidationError('本机最多支持100个账号。')
            if existing and existing['role'] == 'admin' and not existing.get('disabled') and (role != 'admin' or disabled):
                if not any(n != name and u['role'] == 'admin' and not u.get('disabled') for n, u in self.state['users'].items()):
                    raise ValidationError('不能停用或降级最后一个管理员。')
            password = payload.get('password')
            if not existing or password:
                hashed = password_hash(password_value(password))
            else:
                hashed = {k: existing[k] for k in ('salt', 'hash', 'iterations')}
            self.state['users'][name] = {**hashed, 'role': role, 'disabled': disabled,
                                          'generation': (existing or {}).get('generation', 0) + 1}
            self._audit('account_updated_by_' + actor, name)
            self._save()
            self.sessions = {key: value for key, value in self.sessions.items() if value['username'] != name}
            return self.users()
