# app/routes_admin.py
import os
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, redirect, 
                    url_for, flash, session, current_app)
from flask_login import login_required
from app.utils import admin_required
from app import db, scheduler
from app.models import User, Post

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/')
@login_required
@admin_required
def dashboard():
    
    # 1. Метрики (БД)
    users_total = User.query.count()
    posts_total = Post.query.count()
    posts_published = Post.query.filter_by(status='published').count()
    posts_failed = Post.query.filter_by(status='failed').count()
    
    # 2. Статус планировщика
    try:
        scheduler_status = 'RUNNING' if scheduler.running else 'STOPPED'
    except Exception:
        scheduler_status = 'UNKNOWN'

    # 3. Логи (читаем последние 50 строк)
    log_content = ""
    try:
        # Пытаемся найти лог (он может быть в корне или в data/logs)
        log_path = 'app.log'
        if not os.path.exists(log_path):
            log_path = '/var/www/pb_cpbox_ru_usr/data/logs/pb.cpbox.ru-error.log' # (Твой путь на сервере)
            
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                # Читаем файл целиком, берем последние строки
                lines = f.readlines()
                last_lines = lines[-50:] 
                log_content = "".join(reversed(last_lines)) # Свежие сверху
        else:
            log_content = "Файл логов не найден."
    except Exception as e:
        log_content = f"Ошибка чтения лога: {e}"

    # 4. Пользователи
    all_users = User.query.order_by(User.created_at.desc()).all()
    
    # 5. Файлы (Количество и Объем)
    upload_path = current_app.config['UPLOAD_FOLDER']
    media_files_count = 0
    media_total_size_mb = 0.0
    
    if os.path.exists(upload_path):
        files = os.listdir(upload_path)
        media_files_count = len(files)
        
        total_bytes = 0
        for f in files:
            fp = os.path.join(upload_path, f)
            # Пропускаем, если это папка (на всякий случай)
            if os.path.isfile(fp):
                total_bytes += os.path.getsize(fp)
        
        # Переводим байты в мегабайты
        media_total_size_mb = total_bytes / (1024 * 1024)

    # 6. Текущее время (для заголовка)
    now = datetime.utcnow()

    return render_template('admin/dashboard.html',
                           users_total=users_total,
                           posts_total=posts_total,
                           posts_published=posts_published,
                           posts_failed=posts_failed,
                           scheduler_status=scheduler_status,
                           log_content=log_content,
                           all_users=all_users,
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