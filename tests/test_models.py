# tests/test_models.py
from app import db
from app.models import User, Tariff, Project

def test_user_creation(app):
    """Проверка, что юзер создается корректно."""
    user = User(email='new@test.com')
    user.set_password('123')
    db.session.add(user)
    db.session.commit()
    assert user.id is not None
    assert user.check_password('123')

def test_tariff_limits(client, auth_client):
    """Тест проверки лимитов (can_create_project)."""
    client, user = auth_client
    
    # 1. Сейчас тариф MINI (лимит 1 проект)
    # У юзера уже есть 1 проект (создан в фикстуре)
    
    allowed, msg = user.can_create_project()
    assert allowed is False # Должно быть запрещено
    assert "Достигнут лимит" in msg

    # 2. Меняем тариф на PRO (лимит 10)
    pro = Tariff.query.filter_by(slug='pro').first()
    user.tariff_id = pro.id
    db.session.commit()
    
    allowed, msg = user.can_create_project()
    assert allowed is True # Теперь можно