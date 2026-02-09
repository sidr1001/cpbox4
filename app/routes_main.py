# app/routes_main.py
import os
import uuid
import json
from datetime import datetime, timedelta
import pytz
from bs4 import BeautifulSoup
import requests
import calendar
from flask import (Blueprint, render_template, request, redirect, 
                   url_for, flash, current_app, session, abort, jsonify, g) 
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db, scheduler
from app.models import Post, TgChannel, VkGroup, OkGroup, MaxChat, User, SocialTokens, Signature, Project, Tariff
from app.services import (
    publish_post_task, vk_send_service, 
    tg_delete_service, vk_delete_service
)
# , max_send_service
main_bp = Blueprint('main', __name__)

@main_bp.route('/save_initial_settings', methods=['POST'])
@login_required
def save_initial_settings():
    timezone = request.form.get('timezone')
    tariff_id = request.form.get('tariff') # Может быть None, если меняем только таймзону
    
    # Проверка: должно прийти хотя бы одно значение
    if not timezone and not tariff_id:
        flash('Нет данных для сохранения.', 'warning')
        return redirect(request.referrer or url_for('main.index'))

    try:
        # 1. Сохраняем таймзону (если пришла)
        if timezone:
            current_user.timezone = timezone
        
        # 2. Сохраняем тариф (если пришел)
        if tariff_id:
            current_user.tariff_id = int(tariff_id)
            
        # 3. Фиксируем завершение настройки (всегда)
        current_user.is_setup_complete = True 
        
        db.session.commit()
        flash('Настройки сохранены!', 'success')
        
    except ValueError:
        db.session.rollback()
        flash('Ошибка: Некорректный формат тарифа.', 'danger')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Settings save error: {e}")
        flash('Ошибка при сохранении.', 'danger')
        
    # --- ЛОГИКА ВОЗВРАТА ---
    # Пытаемся вернуться туда, откуда пришли (например, в профиль)
    referrer = request.form.get('next') or request.referrer
    if referrer:
        return redirect(referrer)
        
    return redirect(url_for('main.index'))
    
@main_bp.route('/analytics')
@login_required
def analytics():
    # Проверка наличия проекта
    if not g.project:
        return redirect(url_for('main.index'))
	# Получаем период из URL (по умолчанию 7 дней)
    period = request.args.get('period', '7')
    
	# 1. Основные счетчики (за всё время)
    # --- ИЗМЕНЕНИЕ: Фильтруем по project_id ---
    base_query = Post.query.filter_by(project_id=g.project.id)
    
    total_posts = base_query.count()
    published_count = base_query.filter_by(status='published').count()
    scheduled_count = base_query.filter_by(status='scheduled').count()
    failed_count = base_query.filter_by(status='failed').count()

    # 2. Статистика по платформам
    all_user_posts = base_query.all()
    platform_stats = {
        'tg': sum(1 for p in all_user_posts if p.publish_to_tg),
        'vk': sum(1 for p in all_user_posts if p.publish_to_vk),
        'ig': sum(1 for p in all_user_posts if p.publish_to_ig),
        # 'max': sum(1 for p in all_user_posts if p.publish_to_max),
    }

    # 3. Данные для Графика
    dates = []
    counts = []
    today = datetime.utcnow().date()
    
    if period == '365':
        # --- Режим ГОД: Группируем по Месяцам (последние 12) ---
        for i in range(11, -1, -1):
            # Вычисляем год и месяц для итерации
            # (немного математики, чтобы корректно уйти в прошлый год)
            iter_year = today.year
            iter_month = today.month - i
            
            if iter_month <= 0:
                iter_month += 12
                iter_year -= 1
            
            # Находим начало и конец этого месяца
            start_date = datetime(iter_year, iter_month, 1).date()
            # Конец месяца:
            last_day = calendar.monthrange(iter_year, iter_month)[1]
            end_date = datetime(iter_year, iter_month, last_day).date()
            
            # Считаем посты в этом диапазоне
            cnt = base_query.filter(
                Post.created_at >= start_date,
                Post.created_at <= datetime.combine(end_date, datetime.max.time())
            ).count()
            
            # Подпись: "Янв 24" или "01.24"
            label = start_date.strftime('%b %y') # Например Jan 24 (зависит от локали)
            # Если нужна русская локаль без настройки сервера, можно просто цифрами:
            label = f"{iter_month:02d}.{str(iter_year)[-2:]}" 
            
            dates.append(label)
            counts.append(cnt)

    else:
        # --- Режим ДНИ (7 или 30) ---
        try:
            days_count = int(period)
        except:
            days_count = 7 # Фолбэк, если ввели ерунду
            
        for i in range(days_count - 1, -1, -1):
            day = today - timedelta(days=i)
            
            # Фильтр по конкретному дню
            # Используем created_at, так как мы его добавили (Вариант А)
            cnt = base_query.filter(db.func.date(Post.created_at) == day).count()
            
            dates.append(day.strftime('%d.%m'))
            counts.append(cnt)

    return render_template('analytics.html',
                           total=total_posts,
                           published=published_count,
                           scheduled=scheduled_count,
                           failed=failed_count,
                           platform_stats=platform_stats,
                           chart_dates=json.dumps(dates),
                           chart_counts=json.dumps(counts),
                           current_period=period)  

@main_bp.route('/', methods=['GET', 'POST'])
@login_required 
def index():
    # --- ГАРАНТИЯ ПРОЕКТА ---
    # Если у пользователя нет активного проекта или он не выбран, исправляем это
    if not g.project:
        if not current_user.projects:
            # Создаем первый проект
            new_p = Project(user_id=current_user.id, name="Мой проект")
            db.session.add(new_p)
            db.session.commit()
            current_user.current_project_id = new_p.id
            db.session.commit()
        else:
            # Выбираем первый попавшийся
            current_user.current_project_id = current_user.projects[0].id
            db.session.commit()
        # Перезагружаем, чтобы g.project обновился
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        # --- ПРОВЕРКА ТАРИФА ---
        allowed, msg = current_user.can_create_post()
        if not allowed:
            return jsonify({'status': 'error', 'message': msg}), 403
        # -----------------------        
        
        try:
            # --- 1. Сбор данных из формы ---
            text_html_raw = request.form.get('text_html', '') 
            text_vk_plain_form = request.form.get('text_vk', '') 
            use_separate_vk_text = 'separate_vk_text' in request.form
            
            # --- 2. САНАЦИЯ ДАННЫХ ---
            soup_tg = BeautifulSoup(text_html_raw, 'html.parser')
            allowed_tags = ['b', 'strong', 'i', 'em', 'u', 's', 'strike', 'a', 'code', 'pre', 'p', 'br', 'ol', 'ul', 'li']
            
            for tag in soup_tg.find_all(True):
                if tag.name not in allowed_tags:
                    tag.unwrap() 
            
            clean_html = str(soup_tg)

            tg_html = clean_html.replace('<p>', '').replace('</p>', '\n\n')
            tg_html = tg_html.replace('<strong>', '<b>').replace('</strong>', '</b>')
            tg_html = tg_html.replace('<em>', '<i>').replace('</em>', '</i>')
            tg_html = tg_html.replace('<u>', '<u>').replace('</u>', '</u>') 
            tg_html = tg_html.replace('<s>', '<s>').replace('</s>', '</s>') 
            tg_html = tg_html.replace('<strike>', '<s>').replace('</strike>', '</s>')
            tg_html = tg_html.replace('<br>', '\n').replace('<br/>', '\n')
            tg_html = tg_html.replace('<ol>', '').replace('</ol>', '\n')
            tg_html = tg_html.replace('<ul>', '').replace('</ul>', '\n')
            tg_html = tg_html.replace('<li>', '• ').replace('</li>', '\n')
            tg_html = tg_html.strip()

            if use_separate_vk_text:
                vk_text_final = text_vk_plain_form
            else:
                vk_html_fixed = text_html_raw.replace('</p>', '\n').replace('<br>', '\n').replace('<br/>', '\n')
                vk_html_fixed = vk_html_fixed.replace('</li>', '\n')
                soup_vk = BeautifulSoup(vk_html_fixed, 'html.parser')
                vk_text_final = soup_vk.get_text().strip()

            # --- 3. Сбор остальных данных ---
            publish_tg = 'publish_tg' in request.form
            publish_vk = 'publish_vk' in request.form
            publish_ig = 'publish_ig' in request.form
            publish_ok = 'publish_ok' in request.form
            publish_max = 'publish_max' in request.form
            
            tg_channel_id = request.form.get('channel_tg')
            vk_group_id = request.form.get('channel_vk')
            vk_layout = request.form.get('vk_layout', 'grid')
            ok_group_id = request.form.get('channel_ok')
            max_chat_id = request.form.get('channel_max')            
            schedule_at_str = request.form.get('schedule')

            if not vk_text_final and not request.files.getlist('media'):
                return jsonify({'status': 'error', 'message': 'Пост не может быть пустым.'}), 400

            # --- 4. Кнопки ---
            buttons = []
            for t, u in zip(request.form.getlist('button_text'), request.form.getlist('button_url')):
                if t and u:
                    if u.startswith('http'):
                        buttons.append({"text": t, "url": u})
                    else:
                        callback_str = f"user:{current_user.id}|text:{u}"
                        buttons.append({"text": t, "callback_data": callback_str})
            
            # --- 5. Медиа ---
            media_files = [] 
            upload_folder = current_app.config['UPLOAD_FOLDER']
            for f in request.files.getlist('media'):
                if f and f.filename:
                    ext = os.path.splitext(f.filename)[1]
                    safe_name = f"{uuid.uuid4()}{ext}"
                    full_path = os.path.join(upload_folder, safe_name)
                    f.save(full_path)
                    media_files.append(safe_name) 

            # --- 6. ВРЕМЯ (С учетом часового пояса) ---
            scheduled_at_utc = None
            print(f"DEBUG TIME: Получена строка времени: '{schedule_at_str}'")
            
            if schedule_at_str:
                user_tz_str = current_user.timezone or 'UTC'
                try:
                    # 1. Пояс пользователя
                    user_tz = pytz.timezone(user_tz_str)
                    
                    # 2. Парсим (наивное время из формы)
                    dt_naive = datetime.fromisoformat(schedule_at_str)
                    
                    # 3. Присваиваем зону (Локализуем)
                    dt_aware = user_tz.localize(dt_naive)
                    
                    # 4. Переводим в UTC
                    scheduled_at_utc = dt_aware.astimezone(pytz.UTC)
                    
                    print(f"DEBUG TIME: UserTZ={user_tz_str} | Input={dt_naive} | Aware={dt_aware} | UTC={scheduled_at_utc}")
                    
                    # Проверка: не в прошлом ли время?
                    now_utc = datetime.now(pytz.UTC)
                    if scheduled_at_utc < now_utc:
                        print(f"DEBUG TIME: ВНИМАНИЕ! Выбранное время {scheduled_at_utc} меньше текущего {now_utc}. Пост уйдет сразу.")
                    
                except Exception as e:
                    print(f"DEBUG TIME ERROR: {e}")
                    current_app.logger.error(f"Timezone error: {e}")
                    # Фолбэк (на всякий случай, чтобы не упало)
                    scheduled_at_utc = None

            # --- 7. БД: Создаем пост В ТЕКУЩЕМ ПРОЕКТЕ ---
            new_post = Post(
                user_id=current_user.id,
                project_id=g.project.id,
                text=tg_html,
                text_vk=vk_text_final,
                media_files=media_files,
                status='scheduled',
                scheduled_at=scheduled_at_utc, 
                publish_to_tg=publish_tg,
                publish_to_vk=publish_vk,
                publish_to_ig=publish_ig,
                publish_to_ok=publish_ok,
                publish_to_max=publish_max,
                tg_channel_id=tg_channel_id if publish_tg else None,
                vk_group_id=vk_group_id if publish_vk else None,
                ok_group_id=ok_group_id if publish_ok else None,
                max_chat_id=max_chat_id if publish_max else None,
                vk_layout=vk_layout if publish_vk else 'grid',
                platform_info={"buttons": buttons} 
            )
            db.session.add(new_post)
            db.session.commit()
            current_app.logger.info(f"User {current_user.email} created Post {new_post.id}.")

            # --- 8. Запуск ---
            # 8.1. VK (сразу)
            if publish_vk and vk_group_id:
                try:
                    vk_group = VkGroup.query.get(vk_group_id)
                    # Проверяем, что группа принадлежит этому проекту
                    if vk_group and vk_group.project_id == g.project.id:
                        full_paths = [os.path.join(upload_folder, f) for f in media_files]
                        
                        # Получаем токены проекта
                        project_tokens = g.project.tokens

                        # Если токенов нет (новое состояние), нужно обработать это, чтобы не упало с NoneType error
                        if not project_tokens:
                            new_post.error_message = "Ошибка: В проекте не настроены соцсети (нет токенов)."
                            db.session.commit()
                            return jsonify({'status': 'error', 'message': 'Нет токенов в проекте'}), 400

                        post_id, err = vk_send_service(
                            project_tokens,
                            vk_group.group_id,
                            vk_text_final,
                            full_paths,
                            layout=vk_layout, 
                            schedule_at_utc=scheduled_at_utc 
                        )

                        if err: 
                            new_post.error_message = f"VK Error: {err}"
                            # Если была ошибка и это единственная сеть -> ставим failed
                            if not (publish_tg or publish_ig):
                                new_post.status = 'failed'
                        else:
                            p_info = new_post.platform_info or {}
                            p_info['vk_post_id'] = post_id
                            new_post.platform_info = p_info
                            
                            # Если отправляем ТОЛЬКО в VK (и не планируем TG/IG), 
                            # то сразу ставим статус "Опубликовано".
                            if not (publish_tg or publish_ig) and not scheduled_at_utc:
                                new_post.status = 'published'
                                new_post.published_at = datetime.utcnow()

                        db.session.commit()
                except Exception as e:
                    current_app.logger.error(f"VK direct send error: {e}")

            # 8.2 Планировщик (TG, IG, OK, MAX)
            task_id = f"post_{new_post.id}"
            
            # Если время есть — ставим его. Если нет — ставим "сейчас + 1 сек"
            if scheduled_at_utc:
                run_time = scheduled_at_utc
            else:
                run_time = datetime.now(pytz.UTC) + timedelta(seconds=2)
            
            # Добавляем задачу только если выбрана хотя бы одна отложенная сеть
            if publish_tg or publish_ig or publish_ok or publish_max:  
                scheduler.add_job(
                    publish_post_task, 'date',
                    run_date=run_time, 
                    id=task_id, 
                    args=[new_post.id],
                    replace_existing=True
                )

            return jsonify({
                "status": "ok", 
                "message": "Пост принят.",
                "post_id": new_post.id 
            })              
            
            print(f"DEBUG TIME: Задача добавлена в планировщик на {run_time}")            

            if publish_tg or publish_ig or publish_ok or publish_max:  
                run_time = scheduled_at_utc if scheduled_at_utc else (datetime.utcnow() + timedelta(seconds=1))
                scheduler.add_job(
                    publish_post_task, 'date',
                    run_date=run_time, 
                    id=task_id, args=[new_post.id],
                    replace_existing=True
                )               

            return jsonify({
                "status": "ok", 
                "message": "Пост принят.",
                "post_id": new_post.id 
            })
        
        except Exception as e:
            current_app.logger.error(f"Error in index POST: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': f'Server Error: {e}'}), 500

    # --- GET запрос: Загружаем списки ДЛЯ ТЕКУЩЕГО ПРОЕКТА ---
    # БЫЛО: filter_by(user_id=current_user.id)
    # СТАЛО: filter_by(project_id=g.project.id)
    
    tg_channels = TgChannel.query.filter_by(project_id=g.project.id).all()
    vk_groups = VkGroup.query.filter_by(project_id=g.project.id).all()
    
    ok_groups = OkGroup.query.filter_by(project_id=g.project.id).all()
    max_chats = MaxChat.query.filter_by(project_id=g.project.id).all()    
    
    history = Post.query.filter_by(project_id=g.project.id).order_by(Post.id.desc()).limit(50).all()
    
    
    # Подписи пока оставляем общими для юзера, если не нужно иначе
    signatures = Signature.query.filter_by(user_id=current_user.id).all()
    
    show_setup_modal = not current_user.is_setup_complete

    # Определяем текущее время пользователя для отображения
    user_tz_name = current_user.timezone or 'UTC'
    try:
        user_tz = pytz.timezone(user_tz_name)
        user_now = datetime.now(user_tz)
    except Exception:
        user_now = datetime.utcnow()

    return render_template('index.html',
                           telegram_channels=tg_channels,
                           vk_groups=vk_groups,
                           ok_groups=ok_groups,  
                           max_chats=max_chats,                           
                           history=history,
                           signatures=signatures,
                           # has_max_token=bool(current_user.tokens.max_token if current_user.tokens else False),
                           show_setup_modal=show_setup_modal,
                           user_now=user_now,
                           user_timezone=user_tz_name,
                           post_to_edit=None)

@main_bp.route('/delete/<int:post_id>', methods=['POST'])
@login_required
def delete(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        abort(403)
            
    platform_info = post.platform_info or {}
    
    # --- ИСПРАВЛЕНИЕ: Берем токены из проекта ---
    tokens = post.project.tokens if post.project else None
    # ------------------------------------------

    # TG
    tg_msg_id = platform_info.get('tg_msg_id')
    if post.publish_to_tg and post.tg_channel_id and tg_msg_id:
        channel = TgChannel.query.get(post.tg_channel_id)
        if channel and tokens and tokens.tg_token:
            tg_delete_service(tokens.tg_token, channel.chat_id, tg_msg_id)

    # VK
    vk_post_id = platform_info.get('vk_post_id')
    if post.publish_to_vk and post.vk_group_id and vk_post_id:
        group = VkGroup.query.get(post.vk_group_id)
        # Проверяем наличие токенов
        if group and tokens:
            # ВАЖНО: Мы передаем теперь 'tokens', а не 'current_user'
            vk_delete_service(tokens, group.group_id, vk_post_id)

    # Files (Удаление файлов)
    upload_folder = current_app.config['UPLOAD_FOLDER']
    if post.media_files:
        for f in post.media_files:
            try:
                os.remove(os.path.join(upload_folder, f))
            except OSError: pass

    db.session.delete(post)
    db.session.commit()
    
    flash('Пост удален.', 'success')
    return redirect(url_for('main.index'))

@main_bp.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if not (q := data.get('callback_query')):
        return '', 200

    callback_id = q['id']
    callback_data = q.get('data', '') 
    
    user_id = None
    text_to_show = "Ошибка."

    if callback_data.startswith('user:') and '|text:' in callback_data:
        try:
            parts = callback_data.split('|text:', 1)
            user_id = int(parts[0].replace('user:', ''))
            text_to_show = parts[1]
        except Exception: pass

    token = None
    if user_id:
        user = User.query.get(user_id)
        if user and user.tokens:
            token = user.tokens.tg_token 

    if token:
        try:
            requests.post(
                f'https://api.telegram.org/bot{token}/answerCallbackQuery',
                json={"callback_query_id": callback_id, "text": text_to_show, "show_alert": True}, 
                timeout=5
            )
        except Exception: pass
    
    return '', 200

@main_bp.route('/post-status/<int:post_id>')
@login_required
def post_status(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        return jsonify({'status': 'error', 'message': 'Нет доступа'}), 403
        
    status = post.status
    if status == 'published' or status == 'failed':
        # Рендерим user_now для истории
        user_tz_name = current_user.timezone or 'UTC'
        try:
            user_tz = pytz.timezone(user_tz_name)
            user_now = datetime.now(user_tz)
        except:
            user_now = datetime.utcnow()

        html = render_template('_history_item.html', post=post) 
        return jsonify({'status': status, 'html': html, 'error_message': post.error_message})
    
    return jsonify({'status': status})