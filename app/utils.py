# app/utils.py
import os
import hashlib
import hmac
import secrets
import subprocess
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
    
def generate_activation_code(length=6):
    """Генерирует безопасный цифровой код активации фиксированной длины."""
    if length < 4:
        raise ValueError("Длина кода должна быть не меньше 4")
    return ''.join(secrets.choice('0123456789') for _ in range(length))


def hash_activation_code(code: str) -> str:
    """Возвращает SHA-256 хеш кода активации."""
    return hashlib.sha256((code or '').encode('utf-8')).hexdigest()


def verify_activation_code(code: str, code_hash: str) -> bool:
    """Проверяет код активации через сравнение хешей."""
    candidate_hash = hash_activation_code(code)
    return hmac.compare_digest(candidate_hash, code_hash or '')

def optimize_video_file(file_path):
    """
    Сжимает видео с помощью FFmpeg.
    Возвращает имя нового файла (если расширение изменилось) или None при ошибке.
    """
    try:
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
        name, ext = os.path.splitext(filename)
        
        # Всегда конвертируем в .mp4 для совместимости
        new_filename = f"{name}.mp4"
        output_path = os.path.join(directory, new_filename)
        
        # Временный файл, если имя совпадает (например, перезапись mp4)
        if output_path == file_path:
            temp_path = os.path.join(directory, f"{name}_temp.mp4")
        else:
            temp_path = output_path

        # Команда FFmpeg:
        # -vcodec libx264 : видео кодек H.264
        # -crf 28         : коэффициент качества (18-28, где 28 - сильнее сжатие)
        # -preset fast    : скорость кодирования
        # -acodec aac     : аудио кодек
        # -movflags +faststart : оптимизация для веб-проигрывания
        command = [
            '/usr/bin/ffmpeg', '-y',
            '-i', file_path,
            '-vcodec', 'libx264',
            '-crf', '28', 
            '-preset', 'fast',
            '-acodec', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            temp_path
        ]
        
        # Запускаем процесс (глушим вывод, чтобы не мусорить в логах)
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Если сжимали в temp, заменяем оригинал
        if output_path == file_path:
            os.replace(temp_path, output_path)
        else:
            # Если формат изменился (mov -> mp4), удаляем старый mov
            if os.path.exists(file_path):
                os.remove(file_path)
                
        return new_filename
        
    except Exception as e:
        print(f"Video optimization failed: {e}")
        # Если создался мусорный файл - удаляем
        if 'temp_path' in locals() and os.path.exists(temp_path) and temp_path != file_path:
            os.remove(temp_path)
        return None    