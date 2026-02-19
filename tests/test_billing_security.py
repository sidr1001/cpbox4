import base64
import hashlib
import hmac


def test_cloudpayments_webhook_rejects_invalid_signature(client):
    payload = {
        'TransactionId': 'tx-1',
        'AccountId': '1',
        'Amount': '100',
    }
    response = client.post('/billing/cloudpayments/webhook', data=payload)
    assert response.status_code == 403


def test_cloudpayments_webhook_accepts_valid_signature(client, app):
    payload = {
        'TransactionId': 'tx-2',
        'AccountId': '999999',
        'Amount': '100',
    }
    body = b'TransactionId=tx-2&AccountId=999999&Amount=100'
    digest = hmac.new(
        app.config['CLOUDPAYMENTS_API_SECRET'].encode('utf-8'),
        body,
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode('utf-8')

    response = client.post(
        '/billing/cloudpayments/webhook',
        data=payload,
        headers={'Content-HMAC': signature},
    )
    assert response.status_code == 200


def test_unitpay_callback_rejects_invalid_signature(client):
    response = client.get('/billing/unitpay/callback', query_string={
        'method': 'pay',
        'params[account]': '1',
        'params[unitpayId]': 'up-1',
        'params[orderSum]': '100',
        'params[signature]': 'invalid',
    })
    assert response.status_code == 403


def test_unitpay_callback_rejects_invalid_amount(client, app):
    params = {
        'method': 'pay',
        'params[account]': '1',
        'params[unitpayId]': 'up-2',
        'params[orderSum]': '0',
    }

    signature_keys = sorted(params.keys())
    values_str = ''.join(params[k] for k in signature_keys if k.startswith('params[')) + app.config['UNITPAY_SECRET_KEY']
    signature = hashlib.sha256(values_str.encode('utf-8')).hexdigest()
    params['params[signature]'] = signature

    response = client.get('/billing/unitpay/callback', query_string=params)
    assert response.status_code == 400
