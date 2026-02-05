# app/routes_billing.py
import hashlib
import hmac
import json
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required, current_user
from app import db
from app.models import User, Transaction, PromoCode

billing_bp = Blueprint('billing', __name__)
logger = logging.getLogger(__name__)

@billing_bp.route('/topup', methods=['GET'])
@login_required
def topup():
    return render_template('billing/topup.html',
                           cp_public_id=current_app.config['CLOUDPAYMENTS_PUBLIC_ID'],
                           up_public_key=current_app.config['UNITPAY_PUBLIC_KEY'],
                           up_domain=current_app.config['UNITPAY_DOMAIN'])

# --- ПРОВЕРКА ПРОМОКОДА (AJAX) ---
@billing_bp.route('/check_promo', methods=['POST'])
@login_required
def check_promo():
    data = request.get_json()
    code_str = data.get('code', '').strip().upper()
    amount_rub = float(data.get('amount', 0))
    
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
    """
    Рассчитывает, сколько зачислить на баланс, учитывая промокод.
    paid_amount_rub - сколько реально заплатил человек.
    """
    user = User.query.get(user_id)
    if not user:
        logger.error(f"User {user_id} not found for payment")
        return False

    amount_to_credit = paid_amount_rub
    description = f'Пополнение через {provider}'
    
    # Если передан промокод, проверяем его и "восстанавливаем" полную сумму
    if promo_code:
        promo = PromoCode.query.filter_by(code=promo_code).first()
        # ВАЖНО: Проверяем валидность снова (на случай хака)
        if promo and promo.is_active:
            # Обратная логика: мы знаем, сколько он заплатил, нужно понять, сколько хотел пополнить.
            # Но это сложно (из-за округлений). 
            # ПРОЩЕ: Считаем, что скидка - это "бонус" от системы.
            # Баланс = (Сколько заплатил) + (Скидка которую он получил).
            
            discount_val = 0
            if promo.discount_percent > 0:
                # Математика: Paid = Full * (1 - percent/100)
                # Full = Paid / (1 - percent/100)
                factor = 1 - (promo.discount_percent / 100)
                if factor > 0:
                    full_amount = paid_amount_rub / factor
                    discount_val = full_amount - paid_amount_rub
            elif promo.discount_amount > 0:
                # Фикс: просто добавляем скидку обратно
                discount_val = promo.discount_amount / 100
            
            amount_to_credit = paid_amount_rub + discount_val
            description += f' (Промокод {promo.code})'
            
            # Увеличиваем счетчик
            promo.times_used += 1
            
    # Сохраняем в БД (в копейках)
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
    logger.info(f"User {user.id} credited {amount_to_credit} (Paid: {paid_amount_rub})")
    return True

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

    # РАСКОММЕНТИРОВАТЬ В ПРОДЕ:
    # if hmac_header != calculated_hmac_b64:
    #     logger.warning("CP Invalid signature")
    #     return jsonify({"code": 13})

    transaction_id = request.form.get('TransactionId')
    account_id = request.form.get('AccountId')
    amount = float(request.form.get('Amount', 0))
    
    # Получаем метаданные (там лежит промокод)
    # CP передает Data как JSON-строку или объект, зависит от настройки.
    # Flask form обычно парсит это. Пробуем достать.
    promo_code = None
    try:
        data_field = request.form.get('Data')
        if data_field:
            data_json = json.loads(data_field)
            promo_code = data_json.get('promo_code')
    except:
        pass

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

        # РАСКОММЕНТИРОВАТЬ В ПРОДЕ:
        # if request_signature != my_signature:
        #     return jsonify({"error": {"message": "Invalid signature"}}), 400

        # Разбираем Account. Мы передаем его как "USERID_PROMOCODE" или просто "USERID"
        raw_account = params.get('params[account]')
        unitpay_id = params.get('params[unitpayId]')
        order_sum = float(params.get('params[orderSum]', 0))
        
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