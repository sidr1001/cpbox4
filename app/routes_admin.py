# app/routes_admin.py
import os
import json
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, redirect, 
                    url_for, flash, session, current_app, request)
from flask_login import login_required, current_user
from app.utils import admin_required
from app import db, scheduler
from app.models import User, Post, Tariff, Transaction, PromoCode, AppSettings
from sqlalchemy.orm.attributes import flag_modified
from app.services import delete_project_fully

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/')
@login_required
@admin_required
def dashboard():
    
    # --- 1. Логика списка пользователей (Фильтры + Пагинация) ---
    page = request.args.get('page', 1, type=int)
    email_query = request.args.get('email')
    status_filter = request.args.get('status') # 'active', 'inactive', 'all'
    sort_by = request.args.get('sort', 'newest') # 'newest', 'balance_desc', 'balance_asc'
    
    users_q = User.query

    # Поиск по Email
    if email_query:
        users_q = users_q.filter(User.email.ilike(f"%{email_query}%"))
    
    # Фильтр по статусу
    if status_filter == 'active':
        users_q = users_q.filter(User.is_active == True)
    elif status_filter == 'inactive':
        users_q = users_q.filter(User.is_active == False)
        
    # Сортировка (в том числе по балансу)
    if sort_by == 'balance_desc':
        users_q = users_q.order_by(User.balance.desc())
    elif sort_by == 'balance_asc':
        users_q = users_q.order_by(User.balance.asc())
    elif sort_by == 'oldest':
        users_q = users_q.order_by(User.created_at.asc())
    else:
        # По умолчанию новые сверху
        users_q = users_q.order_by(User.created_at.desc())

    # Пагинация (20 пользователей на страницу)
    users_pagination = users_q.paginate(page=page, per_page=20, error_out=False)
    
    # --- 2. Остальные метрики (БД) ---
    # Важно: считаем общие цифры без фильтров для KPI карточек
    users_total = User.query.count()
    posts_total = Post.query.count()
    posts_published = Post.query.filter_by(status='published').count()
    posts_failed = Post.query.filter_by(status='failed').count()
    
    # --- 3. Статус планировщика ---
    try:
        scheduler_status = 'RUNNING' if scheduler.running else 'STOPPED'
    except Exception:
        scheduler_status = 'UNKNOWN'

    # --- 4. Логи (читаем последние 50 строк) ---
    log_content = ""
    try:
        log_path = 'app.log'
        if not os.path.exists(log_path):
            log_path = '/var/www/pb_cpbox_ru_usr/data/logs/pb.cpbox.ru-error.log'
            
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                last_lines = lines[-50:] 
                log_content = "".join(reversed(last_lines))
        else:
            log_content = "Файл логов не найден."
    except Exception as e:
        log_content = f"Ошибка чтения лога: {e}"

    # --- 5. Файлы ---
    upload_path = current_app.config['UPLOAD_FOLDER']
    media_files_count = 0
    media_total_size_mb = 0.0
    
    if os.path.exists(upload_path):
        # Используем os.scandir для ускорения, если файлов много
        with os.scandir(upload_path) as entries:
            for entry in entries:
                if entry.is_file():
                    media_files_count += 1
                    media_total_size_mb += entry.stat().st_size
        
        media_total_size_mb = media_total_size_mb / (1024 * 1024)

    # --- 6. Данные для шаблона ---
    now = datetime.utcnow()
    tariffs = Tariff.query.filter_by(is_active=True).order_by(Tariff.price).all()

    return render_template('admin/dashboard.html',
                           tariffs=tariffs,
                           users_total=users_total,
                           posts_total=posts_total,
                           posts_published=posts_published,
                           posts_failed=posts_failed,
                           scheduler_status=scheduler_status,
                           log_content=log_content,
                           users_pagination=users_pagination,
                           now=now,
                           media_files_count=media_files_count,
                           media_total_size_mb=round(media_total_size_mb, 2))
                           
# --- НОВЫЕ МАРШРУТЫ АДМИНА ---
@admin_bp.route('/user/<int:user_id>/toggle_active', methods=['POST'])
@login_required
@admin_required
def toggle_active(user_id):
    """Активирует или деактивирует пользователя."""
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash('Нельзя деактивировать другого администратора.', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    user.is_active = not user.is_active
    db.session.commit()
    
    status = "активирован" if user.is_active else "деактивирован"
    flash(f'Пользователь {user.email} был {status}.', 'success')
    return redirect(url_for('admin.dashboard'))      

# --- УПРАВЛЕНИЕ ТАРИФАМИ ---

@admin_bp.route('/tariffs')
@login_required
@admin_required
def tariffs_list():
    """Список всех тарифов."""
    tariffs = Tariff.query.order_by(Tariff.price).all()
    return render_template('admin/tariffs.html', tariffs=tariffs)

@admin_bp.route('/tariff/edit/<int:tariff_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def tariff_edit(tariff_id):
    """Редактирование тарифа."""
    tariff = Tariff.query.get_or_404(tariff_id)
    
    if request.method == 'POST':
        try:
            tariff.name = request.form.get('name')
            tariff.slug = request.form.get('slug')
            tariff.price = int(float(request.form.get('price', 0)) * 100)
            tariff.days = int(request.form.get('days', 30))
            tariff.max_projects = int(request.form.get('max_projects', 1))
            tariff.max_posts_per_month = int(request.form.get('max_posts_per_month', 50))
            tariff.is_active = 'is_active' in request.form
            
            # Обработка JSON поля options
            options_json = request.form.get('options')
            if options_json:
                # Проверяем, валидный ли это JSON
                tariff.options = json.loads(options_json)
            else:
                tariff.options = {}

            db.session.commit()
            flash(f'Тариф "{tariff.name}" обновлен!', 'success')
            return redirect(url_for('admin.tariffs_list'))
            
        except ValueError as e:
            db.session.rollback()
            flash(f'Ошибка данных: {e}', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка сохранения: {e}', 'danger')

    # Для удобства редактирования превращаем JSON обратно в строку
    options_str = json.dumps(tariff.options, indent=4, ensure_ascii=False) if tariff.options else '{}'
    
    return render_template('admin/tariff_edit.html', tariff=tariff, options_str=options_str)

@admin_bp.route('/tariff/create', methods=['GET', 'POST'])
@login_required
@admin_required
def tariff_create():
    """Создание нового тарифа."""
    if request.method == 'POST':
        try:
            new_tariff = Tariff(
                name=request.form.get('name'),
                slug=request.form.get('slug'),
                price=int(float(request.form.get('price', 0)) * 100),
                days=int(request.form.get('days', 30)),
                max_projects=int(request.form.get('max_projects', 1)),
                max_posts_per_month=int(request.form.get('max_posts_per_month', 50)),
                is_active='is_active' in request.form,
                options=json.loads(request.form.get('options', '{}'))
            )
            db.session.add(new_tariff)
            db.session.commit()
            flash(f'Тариф "{new_tariff.name}" создан!', 'success')
            return redirect(url_for('admin.tariffs_list'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка создания: {e}', 'danger')
            
    # Используем тот же шаблон редактирования, но передаем пустой объект (или None)
    return render_template('admin/tariff_edit.html', tariff=None, options_str='{\n    "allow_vk": true,\n    "allow_ok": false\n}')
    
# --- ИСТОРИЯ ТРАНЗАКЦИЙ ---
@admin_bp.route('/transactions')
@login_required
@admin_required
def transactions_list():
    """Список всех транзакций с фильтрацией."""
    
    # Параметры фильтрации из URL
    email_filter = request.args.get('email')
    type_filter = request.args.get('type')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    page = request.args.get('page', 1, type=int)
    
    # Базовый запрос с джойном пользователя для поиска по email
    query = Transaction.query.join(User).order_by(Transaction.created_at.desc())
    
    # Применяем фильтры
    if email_filter:
        query = query.filter(User.email.ilike(f"%{email_filter}%"))
        
    if type_filter:
        query = query.filter(Transaction.type == type_filter)
        
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(Transaction.created_at >= dt_from)
        except ValueError: pass
        
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, '%Y-%m-%d')
            # Добавляем 23:59:59 к дате конца
            dt_to = dt_to.replace(hour=23, minute=59, second=59)
            query = query.filter(Transaction.created_at <= dt_to)
        except ValueError: pass

    # Пагинация (50 записей на страницу)
    pagination = query.paginate(page=page, per_page=50, error_out=False)
    transactions = pagination.items
    
    # Для выпадающего списка типов собираем уникальные типы из БД (или хардкодим)
    all_types = db.session.query(Transaction.type).distinct().all()
    types_list = [t[0] for t in all_types]

    return render_template('admin/transactions.html', 
                           transactions=transactions, 
                           pagination=pagination,
                           types_list=types_list)

# --- РУЧНОЕ ИЗМЕНЕНИЕ БАЛАНСА ---
@admin_bp.route('/user/<int:user_id>/adjust_balance', methods=['POST'])
@login_required
@admin_required
def adjust_balance(user_id):
    user = User.query.get_or_404(user_id)
    
    try:
        # Получаем сумму в рублях (float), конвертируем в копейки (int)
        amount_rub = float(request.form.get('amount', 0))
        description = request.form.get('description', 'Ручная корректировка администратором')
        
        if amount_rub == 0:
            flash('Сумма не может быть нулевой.', 'warning')
            return redirect(url_for('admin.dashboard'))

        amount_kopeks = int(amount_rub * 100)
        
        # Обновляем баланс
        user.balance += amount_kopeks
        
        # Создаем транзакцию
        tx = Transaction(
            user_id=user.id,
            amount=amount_kopeks,
            type='manual_correction', # Специальный тип для ручных правок
            description=description
        )
        db.session.add(tx)
        db.session.commit()
        
        flash(f'Баланс пользователя {user.email} успешно изменен на {amount_rub} ₽.', 'success')
        
    except ValueError:
        flash('Некорректная сумма.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка: {e}', 'danger')
        
    return redirect(request.referrer or url_for('admin.dashboard'))    
    
# --- УПРАВЛЕНИЕ ПРОМОКОДАМИ ---

@admin_bp.route('/promocodes')
@login_required
@admin_required
def promocodes_list():
    """Список всех промокодов."""
    promocodes = PromoCode.query.order_by(PromoCode.id.desc()).all()
    return render_template('admin/promocodes.html', promocodes=promocodes, now=datetime.utcnow())
    
@admin_bp.route('/promocode/create', methods=['GET', 'POST'])
@login_required
@admin_required
def promocode_create():
    if request.method == 'POST':
        try:
            code = request.form.get('code', '').strip().upper()
            limit = int(request.form.get('limit', 0))
            
            # Логика типа скидки
            discount_type = request.form.get('discount_type', 'percent')
            discount_percent = 0
            discount_amount = 0
            
            if discount_type == 'percent':
                discount_percent = int(request.form.get('discount_percent', 0))
            else:
                # Конвертируем рубли в копейки
                amount_rub = float(request.form.get('discount_amount', 0))
                discount_amount = int(amount_rub * 100)

            valid_until_str = request.form.get('valid_until')
            valid_until = None
            if valid_until_str:
                valid_until = datetime.strptime(valid_until_str, '%Y-%m-%dT%H:%M')

            if PromoCode.query.filter_by(code=code).first():
                flash(f'Промокод {code} уже существует!', 'danger')
                return redirect(url_for('admin.promocode_create'))

            new_promo = PromoCode(
                code=code,
                discount_percent=discount_percent,
                discount_amount=discount_amount, # Сохраняем фикс
                usage_limit=limit,
                valid_until=valid_until,
                is_active='is_active' in request.form
            )
            db.session.add(new_promo)
            db.session.commit()
            
            flash(f'Промокод {code} создан.', 'success')
            return redirect(url_for('admin.promocodes_list'))
            
        except Exception as e:
            flash(f'Ошибка: {e}', 'danger')

    return render_template('admin/promocode_edit.html', promo=None)

@admin_bp.route('/promocode/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def promocode_edit(id):
    promo = PromoCode.query.get_or_404(id)
    
    if request.method == 'POST':
        try:
            promo.code = request.form.get('code', '').strip().upper()
            promo.usage_limit = int(request.form.get('limit', 0))
            
            # Логика типа скидки
            discount_type = request.form.get('discount_type')
            if discount_type == 'percent':
                promo.discount_percent = int(request.form.get('discount_percent', 0))
                promo.discount_amount = 0
            else:
                amount_rub = float(request.form.get('discount_amount', 0))
                promo.discount_amount = int(amount_rub * 100)
                promo.discount_percent = 0
            
            valid_until_str = request.form.get('valid_until')
            if valid_until_str:
                promo.valid_until = datetime.strptime(valid_until_str, '%Y-%m-%dT%H:%M')
            else:
                promo.valid_until = None
                
            promo.is_active = 'is_active' in request.form
            
            db.session.commit()
            flash(f'Промокод обновлен.', 'success')
            return redirect(url_for('admin.promocodes_list'))
            
        except Exception as e:
            flash(f'Ошибка: {e}', 'danger')

    return render_template('admin/promocode_edit.html', promo=promo)

@admin_bp.route('/promocode/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def promocode_delete(id):
    promo = PromoCode.query.get_or_404(id)
    db.session.delete(promo)
    db.session.commit()
    flash('Промокод удален.', 'success')
    return redirect(url_for('admin.promocodes_list'))   

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings_page():
    # Получаем глобальные настройки
    settings = AppSettings.get_settings()
    
    if request.method == 'POST':
        # --- 1. ПЛАТЕЖНЫЕ СИСТЕМЫ ---
        providers = request.form.getlist('providers')
        settings.active_payment_providers = ",".join(providers)
        
        # --- 2. УВЕДОМЛЕНИЯ ---
        settings.enable_email_payments = (request.form.get('enable_email_payments') is not None)
        settings.enable_email_tariff = (request.form.get('enable_email_tariff') is not None)
        settings.enable_email_posts = (request.form.get('enable_email_posts') is not None)
        
        # Чекбоксы в HTML: если галочка стоит, приходит 'on', если нет — ничего не приходит.
        # Поэтому проверяем наличие ключа в request.form
        settings.enable_registration = 'enable_registration' in request.form        


        db.session.commit()
        flash('Все настройки системы обновлены.', 'success')
        return redirect(url_for('admin.settings_page'))
    
    # Для отображения галочек в шаблоне
    active_list = settings.active_payment_providers.split(',')
    
    return render_template('admin/settings_global.html', 
                           active_list=active_list, 
                           settings=settings) 

@admin_bp.route('/user/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    """Полное удаление пользователя и всех его проектов."""
    # 1. Защита от самоубийства (нельзя удалить себя)
    if user_id == current_user.id:
        flash('Вы не можете удалить свой собственный аккаунт через админку.', 'danger')
        return redirect(url_for('admin.dashboard'))

    user = User.query.get_or_404(user_id)
    
    try:
        email = user.email # Сохраним для сообщения
        
        # 2. Удаляем все проекты пользователя
        # Превращаем в список, чтобы итератор не сломался при удалении
        user_projects = list(user.projects) 
        
        for project in user_projects:
            # Используем нашу мощную функцию очистки из services.py
            delete_project_fully(project.id)
            
        # 3. Удаляем самого пользователя
        # Связанные записи (транзакции, подписи) удалятся каскадно, 
        # так как в моделях прописано cascade="all, delete-orphan"
        db.session.delete(user)
        db.session.commit()
        
        flash(f'Пользователь {email} и все его данные успешно удалены.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting user {user_id}: {e}")
        flash(f'Ошибка при удалении пользователя: {e}', 'danger')

    return redirect(url_for('admin.dashboard'))                           