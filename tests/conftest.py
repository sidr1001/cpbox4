# tests/conftest.py
import pytest
import tempfile
import shutil
import os
from cryptography.fernet import Fernet
from app import create_app, db
from app.models import User, Tariff, Project

@pytest.fixture
def app():
    """Создает приложение с тестовой конфигурацией."""
    
    # 1. Создаем временную папку для загрузок
    upload_dir = tempfile.mkdtemp()
    
    # 2. Генерируем ключ шифрования
    fernet_key = Fernet.generate_key().decode()

    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test_secret',
        'WTF_CSRF_ENABLED': False,
        'UPLOAD_FOLDER': upload_dir,
        'FERNET_KEY': fernet_key,
        'CLOUDPAYMENTS_API_SECRET': 'cp_test_secret',
        'UNITPAY_SECRET_KEY': 'up_test_secret'
    })

    with app.app_context():
        db.create_all()
        
        # --- Создаем базовые тарифы для тестов ---
        # Используем db.session для добавления
        t1 = Tariff(name='MINI', slug='mini', price=0, max_projects=1, options={'allow_vk': False})
        t2 = Tariff(name='PRO', slug='pro', price=50000, days=30, max_projects=10, options={'allow_vk': True})
        db.session.add_all([t1, t2])
        db.session.commit()
        
        yield app
        
        # --- Очистка базы данных после тестов ---
        db.session.remove() # Закрываем сессию
        db.drop_all()       # Удаляем таблицы
    
    # Удаляем временную папку
    shutil.rmtree(upload_dir)

@pytest.fixture
def client(app):
    """Тестовый клиент для отправки запросов."""
    return app.test_client()

@pytest.fixture
def runner(app):
    """Клиент для тестирования CLI команд."""
    return app.test_cli_runner()

@pytest.fixture
def auth_client(client):
    """Клиент, который сразу логинится и возвращает созданного юзера."""
    # Создаем юзера
    user = User(email='test@example.com', is_active=True, balance=100000) # 1000 руб
    user.set_password('password')
    
    # Привязываем тариф MINI (id=1)
    mini = Tariff.query.filter_by(slug='mini').first()
    user.tariff_id = mini.id
    
    db.session.add(user)
    db.session.commit()
    
    # Создаем проект
    proj = Project(user_id=user.id, name="Test Project")
    db.session.add(proj)
    db.session.commit()
    
    user.current_project_id = proj.id
    db.session.commit()

    # Логинимся
    client.post('/login', data={'email': 'test@example.com', 'password': 'password'})
    
    return client, user