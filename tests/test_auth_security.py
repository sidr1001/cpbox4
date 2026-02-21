import time

from app import db
from app import routes_auth
from app.models import User, Tariff, Project
from app.utils import hash_activation_code


def _create_user(email='sec@example.com', is_active=True):
    user = User(email=email, is_active=is_active, balance=0)
    user.set_password('password')
    mini = Tariff.query.filter_by(slug='mini').first()
    user.tariff_id = mini.id
    db.session.add(user)
    db.session.commit()

    project = Project(user_id=user.id, name='Security project')
    db.session.add(project)
    db.session.commit()

    user.current_project_id = project.id
    db.session.commit()
    return user


def _reset_auth_rate_limit_state():
    routes_auth._login_attempts.clear()
    routes_auth._login_blocked_until.clear()
    routes_auth._register_attempts.clear()
    routes_auth._register_blocked_until.clear()
    routes_auth._verify_attempts.clear()
    routes_auth._verify_blocked_until.clear()


def test_login_blocks_external_next_redirect(client):
    _reset_auth_rate_limit_state()
    _create_user()

    response = client.post(
        '/login?next=https://evil.example/phish',
        data={'email': 'sec@example.com', 'password': 'password'},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert 'evil.example' not in response.headers['Location']
    assert response.headers['Location'].endswith('/')


def test_login_allows_internal_next_redirect(client):
    _reset_auth_rate_limit_state()
    _create_user()

    response = client.post(
        '/login?next=/settings/profile',
        data={'email': 'sec@example.com', 'password': 'password'},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/settings/profile')


def test_login_rate_limit_blocks_bruteforce(client):
    _reset_auth_rate_limit_state()
    _create_user('brute@example.com')

    for _ in range(routes_auth.LOGIN_ATTEMPT_MAX_FAILS):
        response = client.post(
            '/login',
            data={'email': 'brute@example.com', 'password': 'wrong'},
            follow_redirects=True,
        )
        assert response.status_code == 200

    blocked_response = client.post(
        '/login',
        data={'email': 'brute@example.com', 'password': 'wrong'},
        follow_redirects=True,
    )

    body = blocked_response.get_data(as_text=True)
    assert 'Слишком много попыток входа. Попробуйте позже.' in body


def test_login_rate_limit_resets_after_success(client):
    _reset_auth_rate_limit_state()
    _create_user('success@example.com')

    for _ in range(routes_auth.LOGIN_ATTEMPT_MAX_FAILS - 1):
        client.post(
            '/login',
            data={'email': 'success@example.com', 'password': 'wrong'},
            follow_redirects=True,
        )

    ok_response = client.post(
        '/login',
        data={'email': 'success@example.com', 'password': 'password'},
        follow_redirects=False,
    )
    assert ok_response.status_code == 302

    bad_after_success = client.post(
        '/login',
        data={'email': 'success@example.com', 'password': 'wrong'},
        follow_redirects=True,
    )
    body = bad_after_success.get_data(as_text=True)
    assert 'Слишком много попыток входа. Попробуйте позже.' not in body


def test_register_normalizes_email_to_lowercase(client):
    _reset_auth_rate_limit_state()

    response = client.post('/register', data={
        'email': '  NewUser@Example.COM  ',
        'password': 'password',
        'form_timestamp': str(time.time() - 10),
    }, follow_redirects=True)

    assert response.status_code == 200
    user = User.query.filter_by(email='newuser@example.com').first()
    assert user is not None


def test_register_rate_limit_blocks_repeated_existing_email(client):
    _reset_auth_rate_limit_state()
    _create_user('exists@example.com')

    payload = {
        'email': 'exists@example.com',
        'password': 'password',
        'form_timestamp': str(time.time() - 10),
    }

    for _ in range(routes_auth.REGISTER_ATTEMPT_MAX_FAILS):
        response = client.post('/register', data=payload, follow_redirects=True)
        assert response.status_code == 200

    blocked = client.post('/register', data=payload, follow_redirects=True)
    assert 'Слишком много попыток регистрации. Попробуйте позже.' in blocked.get_data(as_text=True)


def test_verify_email_rate_limit_blocks_repeated_wrong_codes(client):
    _reset_auth_rate_limit_state()
    user = _create_user('verify@example.com', is_active=False)
    user.activation_code = hash_activation_code('123456')
    user.activation_code_expires_at = routes_auth.datetime.utcnow() + routes_auth.timedelta(minutes=15)
    db.session.commit()

    with client.session_transaction() as sess:
        sess['verification_email'] = 'verify@example.com'

    for _ in range(routes_auth.VERIFY_ATTEMPT_MAX_FAILS):
        response = client.post('/verify-email', data={'code': '000000'}, follow_redirects=True)
        assert response.status_code == 200

    blocked = client.post('/verify-email', data={'code': '000000'}, follow_redirects=True)
    assert 'Слишком много попыток подтверждения. Попробуйте позже.' in blocked.get_data(as_text=True)


def test_register_stores_hashed_activation_code(client):
    _reset_auth_rate_limit_state()

    response = client.post('/register', data={
        'email': 'hashcheck@example.com',
        'password': 'password',
        'form_timestamp': str(time.time() - 10),
    }, follow_redirects=True)

    assert response.status_code == 200
    user = User.query.filter_by(email='hashcheck@example.com').first()
    assert user is not None
    assert user.activation_code is not None
    assert len(user.activation_code) == 64


def test_forgot_password_uses_generic_message(client):
    response = client.post('/forgot-password', data={'email': 'unknown@example.com'}, follow_redirects=True)
    body = response.get_data(as_text=True)
    assert 'Если email существует, ссылка для сброса отправлена.' in body
