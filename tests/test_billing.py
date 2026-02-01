# tests/test_billing.py
from app import db
from app.models import User, Transaction, Tariff

def test_upgrade_tariff(app, auth_client):
    """Тест покупки платного тарифа."""
    client, user = auth_client
    
    # Запоминаем баланс (в фикстуре дали 100000 = 1000р)
    initial_balance = user.balance
    
    # Ищем тариф PRO (цена 50000 = 500р)
    pro_tariff = Tariff.query.filter_by(slug='pro').first()
    
    # Отправляем POST запрос на смену тарифа
    response = client.post('/settings/update_tariff', data={
        'tariff_id': pro_tariff.id
    }, follow_redirects=True)
    
    assert response.status_code == 200
    
    # --- ИСПРАВЛЕНИЕ ---
    # Получаем текст ответа как строку (as_text=True) и ищем в ней
    response_text = response.get_data(as_text=True).lower()
    assert 'подключен' in response_text
    # -------------------
    
    # Обновляем объект юзера из базы
    db.session.refresh(user)
    
    # Проверки
    assert user.tariff_id == pro_tariff.id
    assert user.balance == initial_balance - 50000 # Списалось 500р
    assert user.tariff_expires_at is not None
    
    # Проверка транзакции
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx is not None
    assert tx.amount == -50000
    assert tx.type == 'tariff_payment'

def test_not_enough_money(app, auth_client):
    """Тест нехватки средств."""
    client, user = auth_client
    
    # Обнуляем баланс
    user.balance = 0
    db.session.commit()
    
    pro_tariff = Tariff.query.filter_by(slug='pro').first()
    
    response = client.post('/settings/update_tariff', data={
        'tariff_id': pro_tariff.id
    }, follow_redirects=True)
    
    # --- ИСПРАВЛЕНИЕ ---
    response_text = response.get_data(as_text=True).lower()
    assert 'недостаточно средств' in response_text
    # -------------------
    
    db.session.refresh(user)
    # Тариф не должен измениться (остался MINI)
    assert user.tariff_id != pro_tariff.id