# app/routes_auth.py
import re
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlparse, urljoin
from flask import (Blueprint, render_template, redirect, url_for, 
                   request, flash, current_app)
from flask_login import login_user, logout_user, current_user
from app import db
from app.models import User, SocialTokens, Project, Tariff, AppSettings, UserLoginHistory
from app.utils import generate_token, verify_token 
from app.email import send_email 
from datetime import datetime, timedelta
from flask import session
from app.utils import generate_activation_code, hash_activation_code, verify_activation_code

auth_bp = Blueprint('auth', __name__)


LOGIN_ATTEMPT_WINDOW_SECONDS = 10 * 60
LOGIN_ATTEMPT_MAX_FAILS = 5
LOGIN_ATTEMPT_BLOCK_SECONDS = 15 * 60

REGISTER_ATTEMPT_WINDOW_SECONDS = 60 * 60
REGISTER_ATTEMPT_MAX_FAILS = 5
REGISTER_ATTEMPT_BLOCK_SECONDS = 60 * 60

VERIFY_ATTEMPT_WINDOW_SECONDS = 15 * 60
VERIFY_ATTEMPT_MAX_FAILS = 5
VERIFY_ATTEMPT_BLOCK_SECONDS = 15 * 60

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_login_attempts = defaultdict(deque)
_login_blocked_until = {}
_register_attempts = defaultdict(deque)
_register_blocked_until = {}
_verify_attempts = defaultdict(deque)
_verify_blocked_until = {}
_rate_lock = Lock()


def _extract_client_ip():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',', 1)[0].strip()
    return ip or 'unknown'


def _build_login_key(email):
    normalized_email = (email or '').strip().lower()
    return f"{_extract_client_ip()}::{normalized_email}"


def _is_login_rate_limited(login_key):
    now_ts = time.time()

    with _rate_lock:
        blocked_until = _login_blocked_until.get(login_key)
        if blocked_until and now_ts < blocked_until:
            return True

        if blocked_until and now_ts >= blocked_until:
            _login_blocked_until.pop(login_key, None)
            _login_attempts.pop(login_key, None)

    return False


def _record_failed_login_attempt(login_key):
    _record_failed_attempt(login_key, _login_attempts, _login_blocked_until,
                           LOGIN_ATTEMPT_WINDOW_SECONDS, LOGIN_ATTEMPT_MAX_FAILS,
                           LOGIN_ATTEMPT_BLOCK_SECONDS)


def _clear_login_attempts(login_key):
    _clear_attempts(login_key, _login_attempts, _login_blocked_until)




def _normalize_email(email):
    normalized = (email or '').strip().lower()
    if not normalized or len(normalized) > 120:
        return None
    if not EMAIL_RE.match(normalized):
        return None
    return normalized


def _is_rate_limited(key, blocked_map):
    now_ts = time.time()

    with _rate_lock:
        blocked_until = blocked_map.get(key)
        if blocked_until and now_ts < blocked_until:
            return True

        if blocked_until and now_ts >= blocked_until:
            blocked_map.pop(key, None)

    return False


def _record_failed_attempt(key, attempts_map, blocked_map, window_s, max_fails, block_s):
    now_ts = time.time()

    with _rate_lock:
        attempts = attempts_map[key]

        while attempts and now_ts - attempts[0] > window_s:
            attempts.popleft()

        attempts.append(now_ts)

        if len(attempts) >= max_fails:
            blocked_map[key] = now_ts + block_s
            attempts_map.pop(key, None)


def _clear_attempts(key, attempts_map, blocked_map):
    with _rate_lock:
        attempts_map.pop(key, None)
        blocked_map.pop(key, None)


def _build_register_key(email):
    return f"{_extract_client_ip()}::{email}"


def _build_verify_key(email):
    return f"{_extract_client_ip()}::{email}"

def _is_safe_redirect_url(target):
    if not target:
        return False

    host_url = request.host_url
    ref_url = urlparse(host_url)
    test_url = urlparse(urljoin(host_url, target))

    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    # 1. ПРОВЕРКА: Включена ли регистрация в настройках?
    settings = AppSettings.get_settings()
    if not settings.enable_registration:
        flash('Регистрация новых пользователей временно приостановлена.', 'warning')
        return redirect(url_for('auth.login'))
        
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
        
    if request.method == 'POST':
        # 2. ЗАЩИТА ОТ БОТОВ (Honeypot + Time Trap)
        
        # А) Honeypot: Если скрытое поле заполнено — это бот
        if request.form.get('confirm_email_honeypot'):
            current_app.logger.warning(f"Bot detected (honeypot): {request.remote_addr}")
            return redirect(url_for('main.index')) # Молча выкидываем
            
        # Б) Time Trap: Если заполнили быстрее чем за 3 секунды — это бот
        try:
            # Получаем время загрузки формы из скрытого поля
            form_ts = float(request.form.get('form_timestamp', 0))
            if time.time() - form_ts < 3:
                flash('Вы слишком быстро заполнили форму. Похоже на бота.', 'warning')
                return redirect(url_for('auth.register'))
        except ValueError:
            pass # Если timestamp подделан или отсутствует        
        
        email = _normalize_email(request.form.get('email'))
        password = request.form.get('password')

        if not email:
            flash('Введите корректный email.', 'danger')
            return redirect(url_for('auth.register'))

        register_key = _build_register_key(email)
        if _is_rate_limited(register_key, _register_blocked_until):
            flash('Слишком много попыток регистрации. Попробуйте позже.', 'warning')
            return redirect(url_for('auth.register'))

        if not password:
            _record_failed_attempt(register_key, _register_attempts, _register_blocked_until,
                                   REGISTER_ATTEMPT_WINDOW_SECONDS, REGISTER_ATTEMPT_MAX_FAILS,
                                   REGISTER_ATTEMPT_BLOCK_SECONDS)
            flash('Email и пароль не могут быть пустыми.', 'danger')
            return redirect(url_for('auth.register'))

        if User.query.filter_by(email=email).first():
            _record_failed_attempt(register_key, _register_attempts, _register_blocked_until,
                                   REGISTER_ATTEMPT_WINDOW_SECONDS, REGISTER_ATTEMPT_MAX_FAILS,
                                   REGISTER_ATTEMPT_BLOCK_SECONDS)
            # Анти-enumeration: не подтверждаем факт существования email
            flash('Если email доступен, код подтверждения будет отправлен.', 'info')
            return redirect(url_for('auth.register'))

        # --- НОВАЯ ЛОГИКА РЕГИСТРАЦИИ ---
        new_user = User(email=email, is_active=False) # (пока неактивен)
        new_user.set_password(password)

        # Генерируем код
        code = generate_activation_code(length=6)
        new_user.activation_code = hash_activation_code(code)
        # Код живет 15 минут
        new_user.activation_code_expires_at = datetime.utcnow() + timedelta(minutes=15)

        try:
            # Сделаем первого пользователя админом (и сразу активным)
            if User.query.count() == 0:
                new_user.is_admin = True
                new_user.is_active = True

            # --- ЛОГИКА ТЕСТОВОГО ПЕРИОДА ---
            max_tariff = Tariff.query.order_by(Tariff.price.desc()).first()
            if max_tariff:
                new_user.tariff_id = max_tariff.id
                new_user.tariff_expires_at = datetime.utcnow() + timedelta(days=7)
                new_user.last_tariff_change = datetime.utcnow()
            # --------------------------------

            db.session.add(new_user)
            db.session.flush()

            default_project = Project(user_id=new_user.id, name="Мой проект")
            db.session.add(default_project)
            db.session.flush()

            new_user.current_project_id = default_project.id
            new_tokens = SocialTokens(project_id=default_project.id)
            db.session.add(new_tokens)

            db.session.commit()

            # ОТПРАВКА КОДА (если не админ)
            if not new_user.is_active:
                send_email(
                    new_user.email,
                    'Ваш код подтверждения PostBot',
                    'email/activate.html',
                    code=code
                )

                # Сохраняем email в сессии, чтобы на след. шаге знать, кого проверять
                session['verification_email'] = new_user.email

                flash('Код подтверждения отправлен на ваш email.', 'info')
                return redirect(url_for('auth.verify_email'))

            _clear_attempts(register_key, _register_attempts, _register_blocked_until)
            current_app.logger.info(f"Новый пользователь зарегистрирован: {email}")
            flash('Регистрация прошла успешно! Проверьте email для активации.', 'success')
            return redirect(url_for('auth.login'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"ОШИБКА РЕГИСТРАЦИИ: {e}")
            flash('Произошла ошибка регистрации. Попробуйте позже.', 'danger')
            return redirect(url_for('auth.register'))
        
    return render_template('auth/register.html', now_timestamp=time.time())
    
@auth_bp.route('/verify-email', methods=['GET', 'POST'])
def verify_email():
    # Достаем email из сессии (куда мы его положили при регистрации)
    email = session.get('verification_email')
    if not email:
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        verify_key = _build_verify_key(email)
        if _is_rate_limited(verify_key, _verify_blocked_until):
            flash('Слишком много попыток подтверждения. Попробуйте позже.', 'warning')
            return redirect(url_for('auth.verify_email'))

        user = User.query.filter_by(email=email).first() if email else None
        
        if not user:
            session.pop('verification_email', None)
            flash('Ошибка пользователя.', 'danger')
            return redirect(url_for('auth.register'))
            
        # Проверки
        if not user.activation_code or not verify_activation_code(code, user.activation_code):
            _record_failed_attempt(verify_key, _verify_attempts, _verify_blocked_until,
                                   VERIFY_ATTEMPT_WINDOW_SECONDS, VERIFY_ATTEMPT_MAX_FAILS,
                                   VERIFY_ATTEMPT_BLOCK_SECONDS)
            flash('Неверный код.', 'danger')
        elif user.activation_code_expires_at < datetime.utcnow():
            _record_failed_attempt(verify_key, _verify_attempts, _verify_blocked_until,
                                   VERIFY_ATTEMPT_WINDOW_SECONDS, VERIFY_ATTEMPT_MAX_FAILS,
                                   VERIFY_ATTEMPT_BLOCK_SECONDS)
            flash('Срок действия кода истек. Зарегистрируйтесь заново.', 'warning')
            # Тут можно добавить логику повторной отправки, но для простоты пока так
        else:
            # УСПЕХ
            _clear_attempts(verify_key, _verify_attempts, _verify_blocked_until)
            user.is_active = True
            user.activation_code = None
            user.activation_code_expires_at = None
            db.session.commit()
            
            # Сразу логиним и чистим сессию
            login_user(user)
            session.pop('verification_email', None)
            
            flash('Аккаунт активирован! Добро пожаловать.', 'success')
            return redirect(url_for('main.index'))
            
    return render_template('auth/verify_email.html', email=email)    


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        email = _normalize_email(request.form.get('email'))
        password = request.form.get('password')

        login_key = _build_login_key(email or 'invalid')
        if _is_login_rate_limited(login_key):
            flash('Слишком много попыток входа. Попробуйте позже.', 'warning')
            current_app.logger.warning(f"Rate limit login blocked: key={login_key}")
            return redirect(url_for('auth.login'))
        
        user = User.query.filter_by(email=email).first() if email else None
        
        if user and user.check_password(password):

            if not user.is_active:
                flash('Аккаунт не активирован. Проверьте email.', 'warning')
                return redirect(url_for('auth.login'))
            
            login_user(user, remember=True)
            _clear_login_attempts(login_key)
            
            # --- ЗАПИСЬ ИСТОРИИ ВХОДА ---
            # Пытаемся получить реальный IP, если сайт за прокси (Nginx/Cloudflare)
            ip = _extract_client_ip()
            
            login_record = UserLoginHistory(
                user_id=user.id,
                ip_address=ip,
                user_agent=request.user_agent.string # Вся строка User-Agent
            )
            db.session.add(login_record)
            db.session.commit()
            # ---------------------------

            current_app.logger.info(f"Пользователь {email} вошел в систему. IP: {ip}")
            
            # --- УДАЛЕН ПРОБЛЕМНЫЙ БЛОК ---
            # tokens = user.tokens
            # if not tokens.vk_token and not tokens.tg_token:
            #     flash('Добро пожаловать! Пожалуйста, настройте ваши соцсети.', 'info')
            #     return redirect(url_for('main.index'))
            # ------------------------------
            
            next_page = request.args.get('next')
            if next_page and _is_safe_redirect_url(next_page):
                return redirect(next_page)
            return redirect(url_for('main.index'))
        else:
            _record_failed_login_attempt(login_key)
            flash('Неверный email или пароль.', 'danger')
            return redirect(url_for('auth.login'))
            
    return render_template('auth/login.html')

@auth_bp.route('/logout')
def logout():
    logout_user()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('auth.login'))

# --- НОВЫЙ МАРШРУТ АКТИВАЦИИ ---
@auth_bp.route('/activate/<token>')
def activate_account(token):
    email = verify_token(token, salt='email-confirm')
    if not email:
        flash('Ссылка для активации недействительна или истекла.', 'danger')
        return redirect(url_for('main.index'))
        
    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Пользователь не найден.', 'danger')
        return redirect(url_for('main.index'))
    
    user.is_active = True
    db.session.commit()
    
    flash('Аккаунт успешно активирован! Теперь вы можете войти.', 'success')
    return redirect(url_for('auth.login'))

# --- НОВЫЙ МАРШРУТ (СБРОС ПАРОЛЯ, ШАГ 1) ---
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first() if email else None
        
        if user:
            token = generate_token(user.email, salt='password-reset')
            reset_url = url_for('auth.reset_password', token=token, _external=True)
            send_email(
                user.email,
                'Сброс пароля PostBot',
                'email/reset_password.html', # (создадим этот шаблон)
                reset_url=reset_url
            )

        # Анти-enumeration: единое сообщение для всех случаев
        flash('Если email существует, ссылка для сброса отправлена.', 'info')
        return redirect(url_for('auth.forgot_password'))
        
    return render_template('auth/forgot_password.html')

# --- НОВЫЙ МАРШРУТ (СБРОС ПАРОЛЯ, ШАГ 2) ---
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_token(token, salt='password-reset', max_age=1800) # (30 минут)
    if not email:
        flash('Ссылка для сброса недействительна или истекла.', 'danger')
        return redirect(url_for('auth.login'))
        
    user = User.query.filter_by(email=email).first_or_404()
    
    if request.method == 'POST':
        password = request.form.get('password')
        if not password:
            flash('Пароль не может быть пустым.', 'warning')
            return redirect(url_for('auth.reset_password', token=token))
            
        user.set_password(password)
        db.session.commit()
        flash('Пароль успешно обновлен! Теперь вы можете войти.', 'success')
        return redirect(url_for('auth.login'))
        
    return render_template('auth/reset_password.html', token=token)