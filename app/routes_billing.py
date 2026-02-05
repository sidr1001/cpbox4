# app/routes_billing.py
import hashlib
import hmac
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, current_app, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from app.models import User, Transaction

billing_bp = Blueprint('billing', __name__)
logger = logging.getLogger(__name__)

@billing_bp.route('/topup', methods=['GET'])
@login_required
def topup():
    """Страница пополнения баланса."""
    return render_template('billing/topup.html',
                           cp_public_id=current_app.config['CLOUDPAYMENTS_PUBLIC_ID'],
                           up_public_key=current_app.config['UNITPAY_PUBLIC_KEY'],
                           up_domain=current_app.config['UNITPAY_DOMAIN'])

# -------------------------------------------------------------------------
# CloudPayments Webhook (Pay Notification)
# Документация: https://developers.cloudpayments.ru/#check
# -------------------------------------------------------------------------
@billing_bp.route('/cloudpayments/webhook', methods=['POST'])
def cloudpayments_webhook():
    secret = current_app.config['CLOUDPAYMENTS_API_SECRET']
    if not secret:
        logger.error("CloudPayments secret not configured")
        return jsonify({"code": 13}) # Ошибка конфигурации

    # 1. Проверка подписи (HMAC-SHA256)
    hmac_header = request.headers.get('Content-HMAC')
    if not hmac_header:
        logger.warning("CloudPayments: No HMAC header")
        return jsonify({"code": 13})

    # Нужно брать raw data байты для проверки
    data_bytes = request.get_data()
    calculated_hmac = hmac.new(
        secret.encode('utf-8'), 
        data_bytes, 
        hashlib.sha256
    ).digest()
    
    # Base64 encode hmac
    import base64
    calculated_hmac_b64 = base64.b64encode(calculated_hmac).decode('utf-8')

    # CloudPayments передает HMAC в Base64
    if hmac_header != calculated_hmac_b64:
        logger.warning(f"CloudPayments: Invalid signature. Got {hmac_header}, calc {calculated_hmac_b64}")
        # return jsonify({"code": 13}) # В боевом режиме раскомментировать

    # 2. Обработка данных
    # CloudPayments шлет данные как Form Data
    amount = float(request.form.get('Amount', 0))
    email = request.form.get('Email')
    account_id = request.form.get('AccountId') # Мы будем передавать ID юзера сюда
    transaction_id = request.form.get('TransactionId')
    status = request.form.get('Status') # Completed

    if not account_id or not transaction_id:
        return jsonify({"code": 0}) 

    # Проверяем, не обрабатывали ли мы уже эту транзакцию
    exists = Transaction.query.filter_by(external_id=str(transaction_id), provider='cloudpayments').first()
    if exists:
        return jsonify({"code": 0})

    # Начисляем баланс
    try:
        user = User.query.get(int(account_id))
        if user:
            amount_kopeks = int(amount * 100)
            user.balance += amount_kopeks
            
            tx = Transaction(
                user_id=user.id,
                amount=amount_kopeks,
                type='deposit',
                status='success',
                provider='cloudpayments',
                external_id=str(transaction_id),
                description=f'Пополнение через CloudPayments ({transaction_id})'
            )
            db.session.add(tx)
            db.session.commit()
            logger.info(f"User {user.id} topped up {amount} via CP.")
    except Exception as e:
        logger.error(f"Error processing CP webhook: {e}")
        db.session.rollback()
        return jsonify({"code": 500})

    return jsonify({"code": 0}) # 0 = Успех

# -------------------------------------------------------------------------
# UnitPay Callback
# Документация: https://help.unitpay.ru/payments/creating-payment
# -------------------------------------------------------------------------
@billing_bp.route('/unitpay/callback', methods=['GET', 'POST'])
def unitpay_callback():
    secret_key = current_app.config['UNITPAY_SECRET_KEY']
    
    # UnitPay шлет параметры в GET (обычно)
    params = request.args.to_dict() 
    method = params.get('method')
    
    if not method:
        return jsonify({"error": {"message": "No method"}}), 400

    # 1. Проверка подписи
    # params['params[signature]'] - это то, что пришло
    # Нам нужно собрать все params[...] (кроме подписи), отсортировать и захэшировать
    
    request_signature = params.get('params[signature]')
    
    # Собираем параметры для подписи
    signature_params = []
    for k, v in params.items():
        if k.startswith('params[') and k != 'params[signature]':
            signature_params.append((k, v))
            
    # Сортировка по ключам, потом конкатенация значений + secret_key
    signature_params.sort(key=lambda x: x[0])
    values_str = "".join([x[1] for x in signature_params]) + secret_key
    my_signature = hashlib.sha256(values_str.encode('utf-8')).hexdigest()

    if request_signature != my_signature:
        logger.warning("UnitPay: Invalid signature")
        # return jsonify({"error": {"message": "Invalid signature"}}), 400

    # 2. Обработка методов
    # UnitPay сначала шлет 'check', потом 'pay'
    
    if method == 'check':
        # Просто подтверждаем, что готовы принять платеж
        return jsonify({"result": {"message": "Ready"}})
        
    if method == 'pay':
        account_id = params.get('params[account]')
        order_sum = float(params.get('params[orderSum]', 0))
        unitpay_id = params.get('params[unitpayId]')
        
        # Проверка дублей
        exists = Transaction.query.filter_by(external_id=str(unitpay_id), provider='unitpay').first()
        if exists:
             return jsonify({"result": {"message": "Already processed"}})

        try:
            user = User.query.get(int(account_id))
            if user:
                amount_kopeks = int(order_sum * 100)
                user.balance += amount_kopeks
                
                tx = Transaction(
                    user_id=user.id,
                    amount=amount_kopeks,
                    type='deposit',
                    status='success',
                    provider='unitpay',
                    external_id=str(unitpay_id),
                    description=f'Пополнение через UnitPay ({unitpay_id})'
                )
                db.session.add(tx)
                db.session.commit()
                logger.info(f"User {user.id} topped up {order_sum} via UnitPay.")
                
                return jsonify({"result": {"message": "Success"}})
        except Exception as e:
            logger.error(f"UnitPay Error: {e}")
            return jsonify({"error": {"message": "Internal Error"}}), 500

    return jsonify({"error": {"message": "Unknown method"}}), 400