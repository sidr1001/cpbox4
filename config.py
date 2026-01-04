# config.py
import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

class Config:
    """Класс конфигурации Flask."""
    
    # Секретный ключ для Flask (сессии, cookies)
    SECRET_KEY = os.environ.get('SECRET_KEY')
    
    # Ключ для шифрования токенов
    FERNET_KEY = os.environ.get('FERNET_KEY')
    if not FERNET_KEY:
        raise ValueError("FERNET_KEY не установлен в .env файле!")

    # Конфигурация базы данных
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.yandex.ru')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 465))
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'true').lower() in ['true', 'on', '1']
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'false').lower() in ['true', 'on', '1']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    # Адрес отправителя по умолчанию
    MAIL_DEFAULT_SENDER = ('PostBot', os.environ.get('MAIL_USERNAME'))    

    # Папка для загрузки медиа
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'app/static/uploads')
    
    # Максимальный размер загружаемого файла (например, 20MB)
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024
    
    VK_APP_ID = os.environ.get('VK_APP_ID')
    VK_APP_SECRET = os.environ.get('VK_APP_SECRET')    