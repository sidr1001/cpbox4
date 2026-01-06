# app/routes_auth.py
from flask import (Blueprint, render_template, redirect, url_for, 
                   request, flash, current_app)
from flask_login import login_user, logout_user, current_user
from app import db
from app.models import User, SocialTokens, Project
from app.utils import generate_token, verify_token # <-- Наши утилиты
from app.email import send_email # <-- Наш отправщик

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            flash('Email и пароль не могут быть пустыми.', 'danger')
            return redirect(url_for('auth.register'))

        if User.query.filter_by(email=email).first():
            flash('Этот email уже зарегистрирован.', 'warning')
            return redirect(url_for('auth.register'))

        # --- НОВАЯ ЛОГИКА РЕГИСТРАЦИИ ---
        new_user = User(email=email, is_active=False) # (пока неактивен)
        new_user.set_password(password)
        
        # Сделаем первого пользователя админом (и сразу активным)
        if User.query.count() == 0:
            new_user.is_admin = True
            new_user.is_active = True
            
        try:
            db.session.add(new_user)
            db.session.commit() # Чтобы получить ID юзера
            
            # 1. Создаем Проект по умолчанию
            default_project = Project(user_id=new_user.id, name="Мой проект")
            db.session.add(default_project)
            db.session.commit() # Чтобы получить ID проекта
            
            # 2. Назначаем активный проект юзеру
            new_user.current_project_id = default_project.id
            db.session.add(new_user)
            
            # 3. Создаем Токены для ЭТОГО проекта (а не для юзера)
            new_tokens = SocialTokens(project_id=default_project.id)
            db.session.add(new_tokens)
            
            db.session.commit()            

            # Если не админ, шлем письмо активации
            if not new_user.is_admin:
                token = generate_token(new_user.email, salt='email-confirm')
                confirm_url = url_for('auth.activate_account', token=token, _external=True)
                send_email(
                    new_user.email,
                    'Активируйте ваш аккаунт PostBot',
                    'email/activate.html', # (этот шаблон мы создадим)
                    confirm_url=confirm_url
                )
            
            current_app.logger.info(f"Новый пользователь зарегистрирован: {email}")
            flash('Регистрация прошла успешно! Проверьте email для активации.', 'success')
            return redirect(url_for('auth.login'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"ОШИБКА РЕГИСТРАЦИИ: {e}")
            flash(f'Произошла ошибка: {e}', 'danger')
            return redirect(url_for('auth.register'))
        
    return render_template('auth/register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):

            if not user.is_active:
                flash('Аккаунт не активирован. Проверьте email.', 'warning')
                return redirect(url_for('auth.login'))
            
            login_user(user, remember=True)
            current_app.logger.info(f"Пользователь {email} вошел в систему.")
            
            # --- УДАЛЕН ПРОБЛЕМНЫЙ БЛОК ---
            # tokens = user.tokens
            # if not tokens.vk_token and not tokens.tg_token:
            #     flash('Добро пожаловать! Пожалуйста, настройте ваши соцсети.', 'info')
            #     return redirect(url_for('main.index'))
            # ------------------------------
            
            next_page = request.args.get('next')
            return redirect(next_page or url_for('main.index'))
        else:
            flash('Неверный email или пароль.', 'danger')
            return redirect(url_for('auth.login'))
            
    return render_template('auth/login.html')

@auth_bp.route('/logout')
def logout():
    logout_user()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('auth.login'))

# --- НОВЫЙ МАРШРУТ АКТИВАЦИИ ---
@auth_bp.route('/activate/<token>')
def activate_account(token):
    email = verify_token(token, salt='email-confirm')
    if not email:
        flash('Ссылка для активации недействительна или истекла.', 'danger')
        return redirect(url_for('main.index'))
        
    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Пользователь не найден.', 'danger')
        return redirect(url_for('main.index'))
    
    user.is_active = True
    db.session.commit()
    
    flash('Аккаунт успешно активирован! Теперь вы можете войти.', 'success')
    return redirect(url_for('auth.login'))

# --- НОВЫЙ МАРШРУТ (СБРОС ПАРОЛЯ, ШАГ 1) ---
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        
        if user:
            token = generate_token(user.email, salt='password-reset')
            reset_url = url_for('auth.reset_password', token=token, _external=True)
            send_email(
                user.email,
                'Сброс пароля PostBot',
                'email/reset_password.html', # (создадим этот шаблон)
                reset_url=reset_url
            )
            flash('Ссылка для сброса пароля отправлена на ваш email.', 'info')
        else:
            flash('Пользователь с таким email не найден.', 'warning')
            
        return redirect(url_for('auth.forgot_password'))
        
    return render_template('auth/forgot_password.html')

# --- НОВЫЙ МАРШРУТ (СБРОС ПАРОЛЯ, ШАГ 2) ---
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_token(token, salt='password-reset', max_age=1800) # (30 минут)
    if not email:
        flash('Ссылка для сброса недействительна или истекла.', 'danger')
        return redirect(url_for('auth.login'))
        
    user = User.query.filter_by(email=email).first_or_404()
    
    if request.method == 'POST':
        password = request.form.get('password')
        if not password:
            flash('Пароль не может быть пустым.', 'warning')
            return redirect(url_for('auth.reset_password', token=token))
            
        user.set_password(password)
        db.session.commit()
        flash('Пароль успешно обновлен! Теперь вы можете войти.', 'success')
        return redirect(url_for('auth.login'))
        
    return render_template('auth/reset_password.html', token=token)