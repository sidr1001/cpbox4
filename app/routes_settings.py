# app/routes_settings.py
import requests
import logging
import os         # Для генерации случайных байт
import hashlib    # Для SHA256
import base64     # Для Base64 URL-safe
from flask import (Blueprint, render_template, request, flash, 
                   redirect, url_for, current_app, abort, session, g)
from flask_login import login_required, current_user
from app import db
from app.models import SocialTokens, TgChannel, VkGroup, User, Signature, RssSource, Project
from sqlalchemy.exc import IntegrityError
from app.services import fetch_tg_channels, fetch_vk_groups
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__)

@settings_bp.route('/social', methods=['GET', 'POST'])
@login_required
def social():
    # Проверка проекта
    if not g.project: return redirect(url_for('main.index'))
        
    tokens = SocialTokens.query.filter_by(project_id=g.project.id).first()
    if not tokens:
        # ИЗМЕНЕНИЕ: Создаем с project_id
        tokens = SocialTokens(project_id=g.project.id) 
        db.session.add(tokens)

    if request.method == 'POST':
        # Флаги, чтобы знать, что обновлять
        updated_tg = False
        updated_vk = False
        
        # --- 1. Telegram ---
        tg_token_form = request.form.get('tg_token')
        if tg_token_form and tg_token_form != '***':
            tokens.tg_token = tg_token_form # Сеттер зашифрует
            
            try:
                # Говорим Telegram, куда слать обновления
                webhook_url = url_for('main.webhook', _external=True)
                api_url = f"https://api.telegram.org/bot{tokens.tg_token}/setWebhook"

                resp = requests.post(api_url, json={"url": webhook_url}, timeout=5)

                if resp.ok and resp.json().get('result') == True:
                    flash('Вебхук Telegram успешно установлен!', 'info')
                else:
                    flash(f'Вебхук Telegram НЕ установлен: {resp.text}', 'warning')

            except Exception as e:
                flash(f'Ошибка установки вебхука: {e}', 'danger')            
            
            # Пытаемся проверить токен
            try:
                TG_API_URL = f"https://api.telegram.org/bot{tokens.tg_token}/getMe"
                resp = requests.get(TG_API_URL, timeout=5)
                if resp.ok:
                    bot_name = resp.json()['result']['username']
                    flash(f'Токен Telegram для @{bot_name} успешно сохранен.', 'success')
                else:
                    flash('Токен Telegram сохранен, но не прошел проверку (getMe).', 'warning')
            except Exception as e:
                flash(f'Ошибка проверки токена: {e}', 'danger')
                
        # --- 2. VK ---
        vk_token_form = request.form.get('vk_token')
        if vk_token_form:
            if vk_token_form != '***':
                tokens.vk_token = vk_token_form
                updated_vk = True
        
        # --- 3. Instagram ---
        ig_token_form = request.form.get('ig_page_token')
        ig_user_id_form = request.form.get('ig_user_id')

        if ig_token_form and ig_token_form != '***':
            tokens.ig_page_token = ig_token_form # Используем новый сеттер
        
        if ig_user_id_form:
            tokens.ig_user_id = ig_user_id_form

        db.session.commit()
        
        if (ig_token_form and ig_token_form != '***') or ig_user_id_form:
            flash('Данные Instagram сохранены.', 'success')        
        
        flash('Токены сохранены.', 'success')

        # --- 4. Обновление списков каналов/групп ---
        if updated_tg:
            flash('Обновляю список каналов Telegram...', 'info')
            msg, err = fetch_tg_channels(tokens.tg_token, current_user.id)
            if err:
                flash(f'Ошибка TG: {err}', 'danger')
            else:
                flash(f'Успех TG: {msg}', 'success')

        return redirect(url_for('settings.social'))

    # GET: Фильтруем по ПРОЕКТУ
    tg_channels = TgChannel.query.filter_by(project_id=g.project.id).all()
    vk_groups = VkGroup.query.filter_by(project_id=g.project.id).all()
    rss_sources = RssSource.query.filter_by(project_id=g.project.id).all()
    signatures = Signature.query.filter_by(user_id=current_user.id).all()  
    
    return render_template('settings.html',
                           has_tg_token=bool(tokens.tg_token),
                           has_vk_token=bool(tokens.vk_token),
                           has_ig_token=bool(tokens.ig_page_token),
                           ig_user_id=tokens.ig_user_id,
                           telegram_channels=tg_channels,
                           vk_groups=vk_groups,
                           rss_sources=rss_sources,
                           signatures=signatures)

@settings_bp.route('/tg/add', methods=['POST'])
@login_required
def tg_add():
    if not g.project: return redirect(url_for('main.index'))
    
    name = request.form.get('name')
    chat_id = request.form.get('chat_id')

    if not name or not chat_id:
        flash('Название и Chat ID не могут быть пустыми.', 'danger')
        return redirect(url_for('settings.social'))

    # TODO: Добавить проверку, что chat_id еще не добавлен
    
    new_channel = TgChannel(
        user_id=current_user.id,
        project_id=g.project.id,
        name=name,
        chat_id=chat_id
    )
    db.session.add(new_channel)
    db.session.commit()
    
    flash(f'Канал "{name}" добавлен.', 'success')
    return redirect(url_for('settings.social'))

@settings_bp.route('/tg/delete/<int:channel_id>')
@login_required
def tg_delete(channel_id):
    channel = TgChannel.query.get_or_404(channel_id)
    if channel.user_id != current_user.id: abort(403)
        
    db.session.delete(channel)
    db.session.commit()
    flash(f'Канал "{channel.name}" удален.', 'success')
    return redirect(url_for('settings.social'))  

@settings_bp.route('/vk-auth')
@login_required
def vk_auth():
    app_id = current_app.config.get('VK_APP_ID')
    if not app_id:
        flash('VK_APP_ID не настроен в конфигурации.', 'danger')
        return redirect(url_for('settings.social'))
    
    # 1. Генерируем code_verifier
    # Это случайная строка из 32-128 символов
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    
    # 2. Генерируем code_challenge из verifier
    # code_challenge = BASE64URL-ENCODE(SHA256(code_verifier))
    try:
        challenge_hash = hashlib.sha256(code_verifier.encode('ascii')).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_hash).decode('utf-8').rstrip('=')
    except Exception as e:
        current_app.logger.error(f"Ошибка генерации code_challenge: {e}")
        flash('Критическая ошибка шифрования при авторизации VK.', 'danger')
        return redirect(url_for('settings.social'))
        
    # 3. Генерируем state
    # Это случайная строка для защиты от CSRF
    state = base64.urlsafe_b64encode(os.urandom(16)).decode('utf-8').rstrip('=')
    
    # 4. Сохраняем verifier и state в сессии пользователя
    session['vk_code_verifier'] = code_verifier
    session['vk_auth_state'] = state
    
    # 5. URL, куда VK вернет пользователя
    redirect_uri = url_for('settings.vk_callback', _external=True)
    
    # 6. Формируем URL для авторизации (как ты и просил)
    params = {
        'client_id': app_id,
        'redirect_uri': redirect_uri,
        'scope': 'wall,photos,video,groups,offline', # (email, phone - не нужны для постинга)
        'response_type': 'code',
        'display': 'page',
        'state': state, # (Обязательно для безопасности)
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256'
    }
    
    auth_url = f"https://id.vk.ru/authorize?{requests.compat.urlencode(params)}"
    
    # Логируем (для тебя)
    logger.info(f"DEBUG VK auth URL: {auth_url}")
    
    # Перенаправляем пользователя на сайт VK
    return redirect(auth_url)


# --- ИСПРАВЛЕННЫЙ МАРШРУТ (VK возвращает пользователя сюда) ---
@settings_bp.route('/vk-callback')
@login_required
def vk_callback():
    if not g.project: return redirect(url_for('main.index'))
    
    # 1. Получаем ВСЕ параметры из URL
    code = request.args.get('code')
    state = request.args.get('state')
    device_id = request.args.get('device_id')
    
    # 2. Проверяем state
    saved_state = session.pop('vk_auth_state', None)
    if not saved_state or saved_state != state:
        flash('Ошибка авторизации VK: неверный state.', 'danger')
        return redirect(url_for('settings.social'))
        
    # 3. Проверяем code и device_id
    if not code or not device_id:
        flash(f'Ошибка авторизации VK: не получен code или device_id.', 'danger')
        return redirect(url_for('settings.social'))

    # 4. Достаем code_verifier из сессии
    code_verifier = session.pop('vk_code_verifier', None)
    if not code_verifier:
        flash('Ошибка авторизации VK: сессия истекла (code_verifier не найден).', 'danger')
        return redirect(url_for('settings.social'))

    app_id = current_app.config.get('VK_APP_ID')
    redirect_uri = url_for('settings.vk_callback', _external=True)
    
    try:
        token_url = 'https://id.vk.ru/oauth2/auth' 
        params = {
            'grant_type': 'authorization_code', 
            'client_id': app_id,                
            'redirect_uri': redirect_uri,       
            'code': code,                       
            'code_verifier': code_verifier,     
            'device_id': device_id,             
            'state': state                      
        }

        resp = requests.post(token_url, data=params, timeout=10) 
        resp.raise_for_status() 
        data = resp.json()
        
        if 'error' in data:
            flash(f"Ошибка VK: {data.get('error_description', data['error'])}", 'danger')
            return redirect(url_for('settings.social'))

        # --- V --- ИЗМЕНЕНИЯ ЗДЕСЬ --- V ---
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token') # <-- 1. Получаем refresh_token
        
        # (expires_in = 3600 секунд по умолчанию)
        expires_in = data.get('expires_in', 3600) # <-- 2. Получаем время жизни

        if not access_token or not refresh_token:
            flash('Не удалось получить access_token или refresh_token от VK.', 'danger')
            return redirect(url_for('settings.social'))

        # 5. Сохраняем ВСЕ токены
        tokens = SocialTokens.query.filter_by(project_id=g.project.id).first()
        if not tokens:
            tokens = SocialTokens(project_id=g.project.id)
            db.session.add(tokens)
            
        tokens.vk_token = access_token
        tokens.vk_refresh_token = refresh_token # <-- 3. Сохраняем refresh_token
        tokens.vk_device_id = device_id         # <-- 4. Сохраняем device_id
        
        # 5. Рассчитываем и сохраняем точное время "смерти" токена
        tokens.vk_token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
        
        db.session.commit()
        # --- ^ --- КОНЕЦ ИЗМЕНЕНИЙ --- ^ ---
        
        flash('VK-профиль успешно подключен. Обновляем список групп...', 'success')

        # 6. Обновляем список групп 
        msg, err = fetch_vk_groups(current_user, project_id=g.project.id) 
        if err:
            flash(f'Ошибка VK при получении групп: {err}', 'danger')
        else:
            flash(f'Успех VK: {msg}', 'success')

    except requests.RequestException as e:
        flash(f'Сетевая ошибка при запросе к VK: {e}', 'danger')
        current_app.logger.error(f"VK Token Exchange Error: {e.response.text if e.response else e}")
    
    return redirect(url_for('settings.social'))

@settings_bp.route("/vk/delete/<int:group_id>")
@login_required
def vk_delete(group_id):
    # 1. Удаляем по ID записи в базе (Primary Key), а не по ID группы VK
    group = VkGroup.query.get_or_404(group_id)
    
    # 2. Проверяем владельца
    if group.user_id != current_user.id:
        abort(403)

    try:
        db.session.delete(group)
        db.session.commit()
        flash(f'Группа VK "{group.name}" удалена.', "success")
    except IntegrityError:
        db.session.rollback()
        flash('Не удалось удалить группу: она используется в постах.', 'danger')
    
    return redirect(url_for("settings.social"))
    
# --- ПОДПИСИ ---

@settings_bp.route("/signature/add", methods=["POST"])
@login_required
def signature_add():
    name = request.form.get("name")
    text = request.form.get("text")

    if not name or not text:
        flash("Название и текст подписи обязательны.", "danger")
        return redirect(url_for("settings.social"))

    new_sig = Signature(user_id=current_user.id, name=name, text=text)
    db.session.add(new_sig)
    db.session.commit()

    flash(f'Подпись "{name}" добавлена.', "success")
    return redirect(url_for("settings.social"))

@settings_bp.route("/signature/delete/<int:sig_id>")
@login_required
def signature_delete(sig_id):
    sig = Signature.query.get_or_404(sig_id)
    if sig.user_id != current_user.id:
        abort(403)

    db.session.delete(sig)
    db.session.commit()
    flash(f'Подпись "{sig.name}" удалена.', "success")
    return redirect(url_for("settings.social"))    
    
@settings_bp.route('/rss/add', methods=['POST'])
@login_required
def rss_add():
    url = request.form.get('url')
    name = request.form.get('name')
    
    # Чекбоксы
    pub_tg = 'pub_tg' in request.form
    pub_vk = 'pub_vk' in request.form
    # pub_max = 'pub_max' in request.form
    
    tg_id = request.form.get('tg_channel_id')
    vk_id = request.form.get('vk_group_id')
    
    if not url:
        flash('Ссылка обязательна', 'danger')
        return redirect(url_for('settings.social'))

    new_source = RssSource(
        user_id=current_user.id,
        project_id=g.project.id,
        url=url,
        name=name,
        publish_to_tg=pub_tg,
        tg_channel_id=tg_id if pub_tg else None,
        publish_to_vk=pub_vk,
        vk_group_id=vk_id if pub_vk else None,
        # publish_to_max=pub_max
    )
    
    db.session.add(new_source)
    db.session.commit()
    
    flash('RSS источник добавлен. Посты появятся в течение 15 минут.', 'success')
    return redirect(url_for('settings.social'))

@settings_bp.route('/rss/delete/<int:source_id>')
@login_required
def rss_delete(source_id):
    src = RssSource.query.get_or_404(source_id)
    if src.user_id != current_user.id: abort(403)
    
    db.session.delete(src)
    db.session.commit()
    flash('Источник удален.', 'success')
    return redirect(url_for('settings.social'))    
    
# --- УПРАВЛЕНИЕ ПРОЕКТАМИ ---
@settings_bp.route('/project/switch/<int:project_id>')
@login_required
def switch_project(project_id):
    proj = Project.query.get_or_404(project_id)
    if proj.user_id != current_user.id: abort(403)
        
    current_user.current_project_id = proj.id
    db.session.commit()
    return redirect(request.referrer or url_for('main.index'))

@settings_bp.route('/project/create', methods=['POST'])
@login_required
def create_project():
    name = request.form.get('name')
    if name:
        new_p = Project(user_id=current_user.id, name=name)
        db.session.add(new_p)
        db.session.commit()
        current_user.current_project_id = new_p.id
        db.session.commit()
        flash(f'Проект "{name}" создан!', 'success')
    return redirect(url_for('main.index'))