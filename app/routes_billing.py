# app/routes_billing.py
import hashlib
import hmac
import json
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, current_app, url_for
from flask_login import login_required, current_user
from app import db
import uuid
from yookassa import Configuration, Payment
from app.models import User, Transaction, PromoCode, AppSettings
from app.email import send_email

billing_bp = Blueprint('billing', __name__)
logger = logging.getLogger(__name__)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# Вспомогательная функция для настройки (можно вызывать внутри роутов)
def init_yookassa():
    Configuration.account_id = current_app.config['YOOKASSA_SHOP_ID']
    Configuration.secret_key = current_app.config['YOOKASSA_SECRET_KEY']

@billing_bp.route('/topup', methods=['GET'])
@login_required
def topup():
    # Читаем настройки из БД
    settings = AppSettings.get_settings()
    active_providers = settings.active_payment_providers.split(',')
    
    return render_template('billing/topup.html',
                           active_providers=active_providers,
                           cp_public_id=current_app.config['CLOUDPAYMENTS_PUBLIC_ID'],
                           up_public_key=current_app.config['UNITPAY_PUBLIC_KEY'],
                           up_domain=current_app.config['UNITPAY_DOMAIN'])

# --- ПРОВЕРКА ПРОМОКОДА (AJAX) ---
@billing_bp.route('/check_promo', methods=['POST'])
@login_required
def check_promo():
    data = request.get_json(silent=True) or {}
    code_str = data.get('code', '').strip().upper()
    amount_rub = _safe_float(data.get('amount', 0), default=0.0)

    if amount_rub <= 0:
        return jsonify({'valid': False, 'message': 'Некорректная сумма'})
    
    promo = PromoCode.query.filter_by(code=code_str, is_active=True).first()
    
    # Базовые проверки
    if not promo:
        return jsonify({'valid': False, 'message': 'Промокод не найден'})
    
    if promo.valid_until and promo.valid_until < datetime.utcnow():
        return jsonify({'valid': False, 'message': 'Срок действия истек'})
        
    if promo.usage_limit > 0 and promo.times_used >= promo.usage_limit:
        return jsonify({'valid': False, 'message': 'Лимит использования исчерпан'})

    # Расчет скидки
    discount_rub = 0
    if promo.discount_percent > 0:
        discount_rub = amount_rub * (promo.discount_percent / 100)
    elif promo.discount_amount > 0:
        discount_rub = promo.discount_amount / 100
        
    # Защита от отрицательной суммы
    new_price = max(1, amount_rub - discount_rub)
    
    return jsonify({
        'valid': True,
        'message': f'Применена скидка {round(discount_rub, 2)} ₽',
        'new_price': round(new_price, 2),
        'discount': round(discount_rub, 2)
    })

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ НАЧИСЛЕНИЯ ---
def process_payment(user_id, paid_amount_rub, provider, external_id, promo_code=None):
    user = User.query.get(user_id)
    if not user:
        return False

    current_app.logger.info(f"Start processing: Paid={paid_amount_rub}, Promo={promo_code}")

    amount_to_credit = paid_amount_rub
    description = f'Пополнение через {provider}'
    
    # --- ЛОГИКА ПРОМОКОДОВ ---
    if promo_code:
        # Ищем промокод в БД (без учета регистра)
        promo = PromoCode.query.filter(PromoCode.code.ilike(promo_code)).first()
        
        if promo:
            current_app.logger.info(f"Promo found: {promo.code}, Active: {promo.is_active}")
        else:
            current_app.logger.warning(f"Promo '{promo_code}' NOT found in DB")

        if promo and promo.is_active:
            discount_val = 0
            
            # 1. Скидка в процентах
            if promo.discount_percent > 0:
                factor = 1 - (promo.discount_percent / 100)
                if factor > 0:
                    full_amount = paid_amount_rub / factor
                    discount_val = full_amount - paid_amount_rub
            
            # 2. Скидка фиксированной суммой (ИСПРАВЛЕНИЕ ЗДЕСЬ)
            elif promo.discount_amount > 0:
                # В базе хранятся копейки (например, 10000).
                # Нам нужно перевести их в рубли, чтобы сложить с оплатой.
                discount_val = promo.discount_amount / 100.0 
            
            # Итого: 100 руб (оплата) + 100 руб (бонус) = 200 руб на счет
            amount_to_credit = paid_amount_rub + discount_val
            description += f' (Промокод {promo.code})'
            
            promo.times_used += 1
            current_app.logger.info(f"Promo applied. Bonus: {discount_val}. Total: {amount_to_credit}")
            
    # --- СОХРАНЕНИЕ ---
    try:
        credit_kopeks = int(amount_to_credit * 100)
        user.balance += credit_kopeks
        
        tx = Transaction(
            user_id=user.id,
            amount=credit_kopeks,
            type='deposit',
            status='success',
            provider=provider,
            external_id=str(external_id),
            description=description
        )
        db.session.add(tx)
        db.session.commit()
        
        # --- ОТПРАВКА ПИСЬМА ---
        current_app.logger.info("Transaction saved. Attempting email...")

        # УПРОЩАЕМ ПРОВЕРКУ для теста (убираем get_notification_setting если его нет)
        # Если вы еще не добавили settings в модель User, этот код падал бы.
        # Поэтому сделаем безопасную проверку:
        should_send = True
        if hasattr(user, 'get_notification_setting'):
            should_send = user.get_notification_setting('email_payment_success', True)
        
        if should_send:
            try:
                send_email(
                    to=user.email,
                    subject=f'💰 Баланс пополнен: +{round(amount_to_credit, 2)} ₽',
                    template='email/payment_success.html',
                    user=user,
                    amount=round(amount_to_credit, 2),
                    promo_code=promo_code
                )
                current_app.logger.info(f"Email sent to {user.email}")
            except Exception as e_mail:
                current_app.logger.error(f"Email failed: {e_mail}")
        else:
            current_app.logger.info("Email disabled by user settings")

        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"DB Error processing payment: {e}")
        return False

# --- WEBHOOKS ---

@billing_bp.route('/cloudpayments/webhook', methods=['POST'])
def cloudpayments_webhook():
    secret = current_app.config['CLOUDPAYMENTS_API_SECRET']
    if not secret: return jsonify({"code": 13})

    hmac_header = request.headers.get('Content-HMAC')
    data_bytes = request.get_data()
    calculated_hmac = hmac.new(secret.encode('utf-8'), data_bytes, hashlib.sha256).digest()
    import base64
    calculated_hmac_b64 = base64.b64encode(calculated_hmac).decode('utf-8')

    if not hmac_header or not hmac.compare_digest(hmac_header, calculated_hmac_b64):
        logger.warning("CP Invalid signature")
        return jsonify({"code": 13}), 403

    transaction_id = request.form.get('TransactionId')
    account_id = request.form.get('AccountId')
    amount = _safe_float(request.form.get('Amount', 0), default=0.0)

    if amount <= 0:
        logger.warning("CP Invalid amount")
        return jsonify({"code": 13}), 400
    
    # Получаем метаданные (там лежит промокод)
    # CP передает Data как JSON-строку или объект, зависит от настройки.
    # Flask form обычно парсит это. Пробуем достать.
    promo_code = None
    try:
        data_field = request.form.get('Data')
        if data_field:
            data_json = json.loads(data_field)
            promo_code = data_json.get('promo_code')
    except (TypeError, ValueError, json.JSONDecodeError):
        promo_code = None

    if Transaction.query.filter_by(external_id=str(transaction_id), provider='cloudpayments').first():
        return jsonify({"code": 0})

    try:
        process_payment(int(account_id), amount, 'cloudpayments', transaction_id, promo_code)
    except Exception as e:
        logger.error(f"CP Error: {e}")
        return jsonify({"code": 500})

    return jsonify({"code": 0})

@billing_bp.route('/unitpay/callback', methods=['GET', 'POST'])
def unitpay_callback():
    secret_key = current_app.config['UNITPAY_SECRET_KEY']
    params = request.args.to_dict()
    method = params.get('method')
    
    if method == 'check':
        return jsonify({"result": {"message": "Ready"}})
        
    if method == 'pay':
        request_signature = params.get('params[signature]')
        signature_params = []
        for k, v in params.items():
            if k.startswith('params[') and k != 'params[signature]':
                signature_params.append((k, v))
        signature_params.sort(key=lambda x: x[0])
        values_str = "".join([x[1] for x in signature_params]) + secret_key
        my_signature = hashlib.sha256(values_str.encode('utf-8')).hexdigest()

        if not request_signature or not hmac.compare_digest(request_signature, my_signature):
            logger.warning("UnitPay invalid signature")
            return jsonify({"error": {"message": "Invalid signature"}}), 403

        # Разбираем Account. Мы передаем его как "USERID_PROMOCODE" или просто "USERID"
        raw_account = params.get('params[account]')
        unitpay_id = params.get('params[unitpayId]')
        order_sum = _safe_float(params.get('params[orderSum]', 0), default=0.0)

        if order_sum <= 0:
            return jsonify({"error": {"message": "Invalid amount"}}), 400
        
        user_id = raw_account
        promo_code = None
        
        if '_' in raw_account:
            parts = raw_account.split('_', 1) # Разделяем по первому подчеркиванию
            user_id = parts[0]
            if len(parts) > 1:
                promo_code = parts[1]

        if Transaction.query.filter_by(external_id=str(unitpay_id), provider='unitpay').first():
             return jsonify({"result": {"message": "Already processed"}})

        try:
            process_payment(int(user_id), order_sum, 'unitpay', unitpay_id, promo_code)
            return jsonify({"result": {"message": "Success"}})
        except Exception as e:
            logger.error(f"UnitPay Error: {e}")
            return jsonify({"error": {"message": "Internal Error"}}), 500

    return jsonify({"error": {"message": "Unknown method"}}), 400

# --- YOOKASSA: СОЗДАНИЕ ПЛАТЕЖА ---
@billing_bp.route('/yookassa/create', methods=['POST'])
@login_required
def yookassa_create():
    
    init_yookassa()
    
    data = request.get_json()
    amount = float(data.get('amount', 0))
    promo_code = data.get('promo_code')
    
    current_app.logger.info(f"YooKassa Create: User {current_user.id}, Amount {amount}, Promo: '{promo_code}'")
    
    # Расчет финальной суммы (если есть скидка - логика в process_payment, но тут мы создаем платеж)
    # Для простоты берем сумму, которую прислал фронтенд (уже проверенную через check_promo если надо)
    # В идеале нужно перепроверить промокод здесь, как в check_promo.
    
    if amount < 10:
        return jsonify({'error': 'Минимум 10 рублей'}), 400

    idempotence_key = str(uuid.uuid4())
    
    try:
        payment = Payment.create({
            "amount": {
                "value": str(amount),
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": url_for('settings.profile', _external=True)
            },
            "capture": True,
            "description": f"Пополнение баланса (User ID: {current_user.id})",
            # ВАЖНО: Вот здесь должен быть промокод
            "metadata": {
                "user_id": current_user.id,
                "promo_code": promo_code if promo_code else "" 
            }
        }, idempotence_key)
        
        return jsonify({'confirmation_url': payment.confirmation.confirmation_url})
        
    except Exception as e:
        logger.error(f"YooKassa Create Error: {e}")
        return jsonify({'error': str(e)}), 500

# --- YOOKASSA: WEBHOOK ---
@billing_bp.route('/yookassa/webhook', methods=['POST'])
@billing_bp.route('/yookassa/webhook', methods=['POST'])
def yookassa_webhook():
    event_json = request.json
    if not event_json:
        return jsonify({'code': 400})

    event_type = event_json.get('event')
    obj = event_json.get('object', {})
    yoo_id = obj.get('id')
    
    # 1. Достаем метаданные
    metadata = obj.get('metadata', {})
    user_id = metadata.get('user_id')
    
    # ВАЖНО: Получаем промокод. Если там пусто, будет None
    promo_code_raw = metadata.get('promo_code')
    # Превращаем пустую строку в None, чтобы логика дальше работала верно
    promo_code = promo_code_raw if promo_code_raw else None

    if event_type == 'payment.succeeded':
        # ... получение суммы ...
        amount_dict = obj.get('amount', {})
        value = float(amount_dict.get('value', 0))

        # ЛОГ: Проверяем, видит ли вебхук промокод
        current_app.logger.info(f"Webhook Metadata: User={user_id}, Promo={promo_code}")

        if not user_id:
            return jsonify({'code': 200})

        if Transaction.query.filter_by(external_id=str(yoo_id), provider='yookassa').first():
             return jsonify({'code': 200})

        try:
            # ВАЖНО: Передаем promo_code в функцию
            process_payment(int(user_id), value, 'yookassa', yoo_id, promo_code=promo_code)
        except Exception as e:
            current_app.logger.error(f"YooKassa Process Error: {e}")
            return jsonify({'code': 500})

    # 2. ПЛАТЕЖ ЖДЕТ ПОДТВЕРЖДЕНИЯ (hold)
    elif event_type == 'payment.waiting_for_capture':
        # Так как мы используем capture: True при создании, это событие редкое,
        # но может возникнуть при проверке антифрода.
        logger.info(f"⏳ Payment {yoo_id} is waiting for capture. Check YooKassa dashboard.")
        # Здесь можно добавить логику автоматического подтверждения, если нужно.

    # 3. ОТМЕНА ПЛАТЕЖА
    elif event_type == 'payment.canceled':
        cancellation_details = obj.get('cancellation_details', {})
        reason = cancellation_details.get('reason')
        party = cancellation_details.get('party')
        logger.warning(f"🚫 Payment {yoo_id} CANCELED. Reason: {reason} (by {party})")
        # Баланс мы не начисляли, так что ничего откатывать не нужно.

    # 4. ПРИВЯЗКА КАРТЫ (Сохранение способа оплаты)
    elif event_type == 'payment_method.active':
        payment_method_id = obj.get('id')
        card_info = obj.get('card', {})
        logger.info(f"💳 Payment method saved: {payment_method_id} ({card_info.get('card_type')} *{card_info.get('last4')})")
        # Если вы будете делать рекуррентные платежи (подписки), этот ID нужно сохранить к юзеру.

    # 5. ВОЗВРАТ ДЕНЕГ (Refund)
    elif event_type == 'refund.succeeded':
        # При возврате объект другой, у него есть поле payment_id
        payment_id = obj.get('payment_id')
        amount_dict = obj.get('amount', {})
        refund_amount = float(amount_dict.get('value', 0))
        
        logger.info(f"💸 Refund Succeeded: {refund_amount} RUB for Payment {payment_id}")
        
        # Находим оригинальную транзакцию пополнения, чтобы понять, с кого списать
        original_tx = Transaction.query.filter_by(external_id=payment_id, provider='yookassa').first()
        
        # Проверяем, не обрабатывали ли мы этот возврат ранее (у возврата свой ID - yoo_id)
        existing_refund = Transaction.query.filter_by(external_id=yoo_id, type='refund').first()
        
        if original_tx and not existing_refund:
            try:
                user = User.query.get(original_tx.user_id)
                if user:
                    # Списываем с баланса (превращаем рубли в копейки)
                    amount_kopeks = int(refund_amount * 100)
                    user.balance -= amount_kopeks
                    
                    # Записываем транзакцию возврата
                    refund_tx = Transaction(
                        user_id=user.id,
                        amount=-amount_kopeks, # Отрицательная сумма
                        type='refund',
                        status='success',
                        provider='yookassa',
                        external_id=str(yoo_id), # ID возврата, а не платежа
                        description=f'Возврат средств (Refund) по платежу {payment_id}'
                    )
                    db.session.add(refund_tx)
                    db.session.commit()
                    logger.info(f"✅ Refund processed: User {user.id} balance deducted.")
            except Exception as e:
                logger.error(f"❌ Error processing refund: {e}")
        else:
            if not original_tx:
                logger.warning(f"Refund skipped: Original transaction {payment_id} not found in DB.")
            elif existing_refund:
                logger.info(f"Refund {yoo_id} already processed.")

    return jsonify({'code': 200})    
