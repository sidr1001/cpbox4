# app/utils.py
import os
from functools import wraps
from cryptography.fernet import Fernet
from flask import current_app, abort, redirect, url_for
from flask_login import current_user
from itsdangerous import URLSafeTimedSerializer

# --- Шифрование ---

_fernet = None

def get_fernet():
    """Инициализирует и возвращает экземпляр Fernet из config."""
    global _fernet
    if _fernet:
        return _fernet
    
    key = current_app.config.get('FERNET_KEY')
    if not key:
        raise ValueError("FERNET_KEY не установлен в app.config!")
    
    _fernet = Fernet(key.encode())
    return _fernet

def encrypt_data(data: str) -> str:
    """Шифрует строку."""
    if not data:
        return ""
    return get_fernet().encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """Дешифрует строку. Возвращает пустую строку при ошибке."""
    if not encrypted_data:
        return ""
    try:
        return get_fernet().decrypt(encrypted_data.encode()).decode()
    except Exception:
        current_app.logger.warning("Не удалось дешифровать данные. Ключ мог измениться.")
        return "" 

# --- Декораторы (проверка прав) ---

def admin_required(f):
    """
    Декоратор, который проверяет, что у
    текущего пользователя есть флаг is_admin.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            # Если анонимный пользователь
            return redirect(url_for('auth.login')) 
        
        if not current_user.is_admin:
            # Если вошел, но не админ
            current_app.logger.warning(f"Пользователь {current_user.username} (ID: {current_user.id}) "
                                       f"попытался получить доступ к админ-ресурсу.")
            abort(403) # 403 Forbidden (Нет доступа)
            
        return f(*args, **kwargs)
    return decorated_function
    
def generate_token(email, salt='email-confirm'):
    """Генерирует безопасный токен."""
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(email, salt=salt)

def verify_token(token, salt='email-confirm', max_age=3600):
    """Проверяет токен. Возвращает email или None."""
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = s.loads(
            token,
            salt=salt,
            max_age=max_age # (Токен "живет" 1 час)
        )
    except Exception:
        return None
    return email