# app/email.py
from threading import Thread
from flask import current_app, render_template
from flask_mail import Message
from app import mail

def send_async_email(app, msg):
    """Фоновая задача для отправки email."""
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            current_app.logger.error(f"Ошибка отправки email: {e}")

def send_email(to, subject, template, **kwargs):
    """Главная функция отправки email."""
    # current_app._get_current_object() нужен для передачи
    # контекста приложения в фоновый поток.
    app = current_app._get_current_object()
    
    msg = Message(
        subject,
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[to]
    )
    msg.html = render_template(template, **kwargs)
    
    # Запускаем отправку в отдельном потоке,
    # чтобы не "вешать" веб-запрос.
    thr = Thread(target=send_async_email, args=[app, msg])
    thr.start()
    return thr