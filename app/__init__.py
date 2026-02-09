# app/__init__.py
import os
import logging
import pytz # Импортируем pytz сразу здесь
from pytz import utc
from flask import Flask, render_template, g
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from flask_mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
from config import Config

# --- Инициализация расширений ---
db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate() # Создаем экземпляр Migrate
mail = Mail()
# Принудительно устанавливаем часовой пояс планировщика на UTC
scheduler = BackgroundScheduler(daemon=True, timezone=utc)

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
        # Запуск тестов
        app.config.from_mapping(test_config)

    # --- Настройка логирования ---
    logging.basicConfig(level=logging.INFO,
                        filename='app.log',
                        format='%(asctime)s %(levelname)s %(message)s')
    
    # --- Инициализация расширений с приложением ---
    db.init_app(app)
    migrate.init_app(app, db) # Инициализируем миграции!
    login_manager.init_app(app)
    mail.init_app(app)
    
    # Убедимся, что папка для загрузок существует
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # --- Глобальный контекст пользователя и проекта ---
    @app.before_request
    def load_project():
        # Импорт моделей внутри функции, чтобы избежать циклических ссылок,
        # если models.py импортирует db из этого файла.
        from app.models import Project 
        
        if current_user.is_authenticated and current_user.current_project_id:
            g.project = db.session.get(Project, current_user.current_project_id)
        else:
            g.project = None

    # --- Регистрация маршрутов (Blueprints) ---
    # Важно импортировать модели, чтобы Alembic (Migrate) их увидел
    from . import models 

    from .routes_auth import auth_bp
    app.register_blueprint(auth_bp)

    from .routes_main import main_bp
    app.register_blueprint(main_bp)

    from .routes_settings import settings_bp
    app.register_blueprint(settings_bp, url_prefix='/settings')

    from .routes_admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')
    
    from .routes_billing import billing_bp
    app.register_blueprint(billing_bp, url_prefix='/billing')
    
    # --- Обработчики ошибок ---
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('404.html'), 404
        
    @app.errorhandler(500)
    def internal_server_error(e):
        return render_template('500.html'), 500   

    # --- Запуск планировщика ---
    # Проверка scheduler.running нужна, чтобы при перезагрузке Flask в режиме debug
    # не запускалось два экземпляра планировщика.
    if not scheduler.running:
        from app.services_rss import parse_rss_feeds
        from app.services import check_expired_tariffs
        
        # Добавляем задачу проверки RSS каждые 15 минут
        # replace_existing=True обновляет задачу при перезапуске кода
        scheduler.add_job(id='rss_job', func=parse_rss_feeds, trigger='interval', minutes=15, replace_existing=True)
            
        # Биллинг раз в час
        scheduler.add_job(id='billing_job', func=check_expired_tariffs, trigger='interval', hours=1, replace_existing=True)            
        
        scheduler.start()
        logging.info("Планировщик APScheduler запущен.")

    # --- Фильтр времени (ДО return app!) ---
    @app.template_filter('user_time')
    def user_time_filter(dt, fmt='%d.%m.%Y %H:%M'):
        """Конвертирует UTC время из БД в часовой пояс текущего пользователя."""
        if not dt:
            return ""
        
        # 1. Определяем целевую таймзону (по умолчанию UTC)
        tz_name = 'UTC'
        if current_user.is_authenticated and current_user.timezone:
            tz_name = current_user.timezone
            
        try:
            # Получаем объект таймзоны пользователя
            user_tz = pytz.timezone(tz_name)
            
            # Если дата "наивная" (без таймзоны), считаем её UTC
            if dt.tzinfo is None:
                dt = pytz.utc.localize(dt)
            
            # Переводим в зону пользователя
            local_dt = dt.astimezone(user_tz)
            return local_dt.strftime(fmt)
        except Exception:
            return dt.strftime(fmt)

    # --- Создание таблиц ---
    # При использовании Flask-Migrate это обычно не нужно, но для старта удобно
    with app.app_context():
        db.create_all()

    return app