# app/__init__.py
import os
import logging
from pytz import utc
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
from config import Config

# --- Инициализация расширений ---
# Мы создаем экземпляры здесь, но настраиваем их
# внутри функции create_app, чтобы избежать циклических импортов.

db = SQLAlchemy()
login_manager = LoginManager()
# Принудительно устанавливаем часовой пояс планировщика на UTC
scheduler = BackgroundScheduler(daemon=True, timezone=utc)
mail = Mail()

# Настройка для Flask-Login:
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Пожалуйста, войдите, чтобы получить доступ к этой странице.'
login_manager.login_message_category = 'info'

def create_app(test_config=None):
    """Фабрика для создания экземпляра приложения Flask."""
    
    app = Flask(__name__)
    if test_config is None:
        # Обычный запуск
        app.config.from_object(Config)
    else:
        # Запуск тестов (передаем настройки вручную)
        app.config.from_mapping(test_config)

    # --- Настройка логирования ---
    logging.basicConfig(level=logging.INFO,
                        filename='app.log',
                        format='%(asctime)s %(levelname)s %(message)s')
    
    # --- Инициализация расширений с приложением ---
    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    
    # Убедимся, что папка для загрузок существует
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Регистрируем обработчик перед каждым запросом
    from flask_login import current_user # Импорт нужен здесь
    
    @app.before_request
    def load_project():
        if current_user.is_authenticated and current_user.current_project_id:
            # Сохраняем активный проект в глобальную переменную g на время запроса
            from flask import g
            from app.models import Project
            g.project = db.session.get(Project, current_user.current_project_id)
        else:
            from flask import g
            g.project = None

    # --- Регистрация маршрутов (Blueprints) ---
    
    from . import models # Важно импортировать модели, чтобы Alembic (Migrate) их увидел

    from .routes_auth import auth_bp
    app.register_blueprint(auth_bp)

    from .routes_main import main_bp
    app.register_blueprint(main_bp)

    from .routes_settings import settings_bp
    app.register_blueprint(settings_bp, url_prefix='/settings')

    from .routes_admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # --- Запуск планировщика ---
    if not scheduler.running:
        scheduler.start()
        
        from app.services_rss import parse_rss_feeds
        from app.services import check_expired_tariffs
        
        # Добавляем задачу проверки RSS каждые 15 минут
        if not scheduler.get_job('rss_job'):
            scheduler.add_job(id='rss_job', func=parse_rss_feeds, trigger='interval', minutes=15)
            
        # Биллинг раз в час (или раз в сутки)
        if not scheduler.get_job('billing_job'):
            scheduler.add_job(id='billing_job', func=check_expired_tariffs, trigger='interval', hours=1)            
        
        logging.info("Планировщик APScheduler запущен.")

    # --- Создание БД (если нужно) ---
    with app.app_context():
        db.create_all()

    return app