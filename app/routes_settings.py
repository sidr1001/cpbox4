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
from app.models import SocialTokens, TgChannel, VkGroup, User, Signature, RssSource, Project, Post, RssSource, OkGroup, MaxChat, Tariff, Transaction
from sqlalchemy.exc import IntegrityError
from app.services import fetch_tg_channels, fetch_vk_groups, fetch_ok_groups, clear_tg_data, clear_vk_data, clear_ok_data, clear_max_data, delete_project_fully
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
                
        # --- OK ---
        ok_token = request.form.get('ok_token')
        ok_pub = request.form.get('ok_pub')
        ok_secret = request.form.get('ok_secret')
        
        if ok_token and ok_token != '***': tokens.ok_token = ok_token
        if ok_pub: tokens.ok_app_pub_key = ok_pub
        if ok_secret: tokens.ok_app_secret_key = ok_secret
        
        # --- MAX ---
        max_token = request.form.get('max_token')
        if max_token:
            # Проверка перед сохранением
            if not current_user.get_limit('allow_max'): # Если есть такая опция
                 flash('Тариф не позволяет использовать MAX.', 'warning')
            else:
                 if max_token != '***': tokens.max_token = max_token                

        return redirect(url_for('settings.social'))

    # GET: Фильтруем по ПРОЕКТУ
    tg_channels = TgChannel.query.filter_by(project_id=g.project.id).all()
    vk_groups = VkGroup.query.filter_by(project_id=g.project.id).all()

    ok_groups = OkGroup.query.filter_by(project_id=g.project.id).all()
    max_chats = MaxChat.query.filter_by(project_id=g.project.id).all()

    rss_sources = RssSource.query.filter_by(project_id=g.project.id).all()
    signatures = Signature.query.filter_by(user_id=current_user.id).all()    
    
    return render_template('settings.html',
                           has_tg_token=bool(tokens.tg_token),
                           has_vk_token=bool(tokens.vk_token),
                           has_ig_token=bool(tokens.ig_page_token),
                           ig_user_id=tokens.ig_user_id,
                           telegram_channels=tg_channels,
                           vk_groups=vk_groups,
                           ok_groups=ok_groups,
                           max_chats=max_chats,
                           tokens=tokens,                           
                           rss_sources=rss_sources,
                           signatures=signatures)
                           
@settings_bp.route('/social/disconnect/<string:platform>', methods=['POST'])
@login_required
def disconnect_social(platform):
    # Проверка, что проект выбран
    if not g.project:
        return redirect(url_for('main.index'))
    
    # Ищем токены текущего проекта
    tokens = SocialTokens.query.filter_by(project_id=g.project.id).first()
    if not tokens:
        flash('Настройки не найдены.', 'warning')
        return redirect(url_for('settings.social'))

    # Очищаем поля в зависимости от платформы
    if platform == 'tg':
        tokens.tg_token = None
    elif platform == 'vk':
        tokens.vk_token = None
        tokens.vk_refresh_token = None
        tokens.vk_device_id = None
        tokens.vk_token_expires_at = None
    elif platform == 'ig':
        tokens.ig_page_token = None
        tokens.ig_user_id = None
    # Если вы добавите MAX в модели, добавьте блок и для него
    
    db.session.commit()
    flash(f'Настройки {platform.upper()} успешно удалены.', 'success')
    return redirect(url_for('settings.social'))                           

@settings_bp.route('/tg/add', methods=['POST'])
@login_required
def tg_add():
    # --- ПРОВЕРКА ТАРИФА ---
    if not current_user.get_limit('allow_tg'):
        flash('Ваш тариф не позволяет подключать Telegram каналы. Обновите тариф!', 'danger')
        return redirect(url_for('settings.social'))
    # -----------------------    
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

    # 1. Unlink posts associated with this channel
    Post.query.filter_by(tg_channel_id=channel.id).update({'tg_channel_id': None})
    
    # 2. Unlink RSS sources associated with this channel
    RssSource.query.filter_by(tg_channel_id=channel.id).update({'tg_channel_id': None})

    db.session.delete(channel)
    db.session.commit()
    flash(f'Канал "{channel.name}" удален.', 'success')
    return redirect(url_for('settings.social')) 

@settings_bp.route('/vk-auth')
@login_required
def vk_auth():
    # --- ПРОВЕРКА ТАРИФА ---
    if not current_user.get_limit('allow_vk'):
        flash('Ваш тариф не позволяет публиковать в ВКонтакте. Обновите тариф!', 'danger')
        return redirect(url_for('settings.social'))
    # -----------------------    
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
    publish_ok = 'publish_ok' in request.form
    
    tg_id = request.form.get('tg_channel_id')
    vk_id = request.form.get('vk_group_id')    
    ok_group_id = request.form.get('channel_ok')    
    
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
        publish_to_ok=publish_ok,
        ok_group_id=ok_group_id        
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
    # --- ПРОВЕРКА ТАРИФА ---
    allowed, msg = current_user.can_create_project()
    if not allowed:
        flash(msg, 'danger')
        return redirect(url_for('main.index'))
    # -----------------------    
    name = request.form.get('name')
    if name:
        new_p = Project(user_id=current_user.id, name=name)
        db.session.add(new_p)
        db.session.commit()
        current_user.current_project_id = new_p.id
        db.session.commit()
        flash(f'Проект "{name}" создан!', 'success')
    return redirect(url_for('main.index'))   

@settings_bp.route('/ok/add_group', methods=['POST'])
@login_required
def ok_add_group():
    if not g.project: return redirect(url_for('main.index'))
    name = request.form.get('name')
    gid = request.form.get('group_id')
    
    if name and gid:
        db.session.add(OkGroup(project_id=g.project.id, name=name, group_id=gid))
        db.session.commit()
        flash('Группа OK добавлена', 'success')
    return redirect(url_for('settings.social'))

@settings_bp.route('/max/add_chat', methods=['POST'])
@login_required
def max_add_chat():
    if not g.project: return redirect(url_for('main.index'))
    name = request.form.get('name')
    cid = request.form.get('chat_id')
    
    if name and cid:
        db.session.add(MaxChat(project_id=g.project.id, name=name, chat_id=cid))
        db.session.commit()
        flash('Чат MAX добавлен', 'success')
    return redirect(url_for('settings.social')) 

# --- ОДНОКЛАССНИКИ: АВТОРИЗАЦИЯ ---

@settings_bp.route('/ok-auth')
@login_required
def ok_auth():
    """Перенаправляет пользователя на страницу авторизации OK."""
    # --- ПРОВЕРКА ТАРИФА ---
    if not current_user.get_limit('allow_ok'):
        flash('Ваш тариф не позволяет публиковать в Одноклассники. Обновите тариф!', 'danger')
        return redirect(url_for('settings.social'))
    # -----------------------    
    client_id = current_app.config.get('OK_CLIENT_ID')
    if not client_id:
        flash('OK_CLIENT_ID не настроен на сервере.', 'danger')
        return redirect(url_for('settings.social'))
        
    redirect_uri = url_for('settings.ok_callback', _external=True)
    
    # Права: VALUABLE_ACCESS (бессрочный токен), GROUP_CONTENT (постинг в группы)
    scope = 'VALUABLE_ACCESS;GROUP_CONTENT;PHOTO_CONTENT;VIDEO_CONTENT'
    
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': scope,
        'layout': 'w' # веб-версия
    }
    
    auth_url = f"https://connect.ok.ru/oauth/authorize?{requests.compat.urlencode(params)}"
    return redirect(auth_url)


@settings_bp.route('/ok-callback')
@login_required
def ok_callback():
    """Обрабатывает ответ от OK, получает токен и группы."""
    if not g.project: return redirect(url_for('main.index'))
    
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        flash(f'Ошибка авторизации OK: {error}', 'danger')
        return redirect(url_for('settings.social'))
    if not code:
        flash('Не получен код авторизации от OK.', 'danger')
        return redirect(url_for('settings.social'))
        
    # 1. Меняем код на токен
    client_id = current_app.config.get('OK_CLIENT_ID')
    client_secret = current_app.config.get('OK_CLIENT_SECRET')
    redirect_uri = url_for('settings.ok_callback', _external=True)
    
    try:
        resp = requests.post('https://api.ok.ru/oauth/token.do', data={
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }, timeout=15)
        
        data = resp.json()
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        
        if not access_token:
            flash(f'Не удалось получить токен OK: {data}', 'danger')
            return redirect(url_for('settings.social'))
            
        # 2. Сохраняем токен в проект
        tokens = SocialTokens.query.filter_by(project_id=g.project.id).first()
        if not tokens:
            tokens = SocialTokens(project_id=g.project.id)
            db.session.add(tokens)
            
        tokens.ok_token = access_token
        if refresh_token:
            tokens.ok_refresh_token = refresh_token        
        
        # Публичный и секретный ключи приложения у нас в конфиге, 
        # но для работы сервисов нам может понадобиться сохранить их или использовать глобальные.
        # В нашей модели SocialTokens мы добавили поля ok_app_pub_key. 
        # Если мы используем одно приложение на всех, можно их заполнить из конфига:
        tokens.ok_app_pub_key = current_app.config.get('OK_APP_PUB_KEY')
        tokens.ok_app_secret_key = current_app.config.get('OK_CLIENT_SECRET') # Используем Secret App как Secret Session
        
        db.session.commit()
        
        flash('OK подключен успешно! Загружаю список групп...', 'success')
        
        # 3. Загружаем группы
        msg, err = fetch_ok_groups(g.project.id)
        if err:
            flash(f'Предупреждение: {err}', 'warning')
        else:
            flash(f'Готово: {msg}', 'success')
            
    except Exception as e:
        flash(f'Ошибка соединения с OK: {e}', 'danger')
        logger.error(f"OK Callback Error: {e}", exc_info=True)
        
    return redirect(url_for('settings.social'))    
    
@settings_bp.route('/ok/delete/<int:group_id>')
@login_required
def ok_delete(group_id):
    # Проверка наличия проекта
    if not g.project:
        return redirect(url_for('main.index'))
    
    # 1. Получаем группу по ID (Primary Key)
    group = OkGroup.query.get_or_404(group_id)
    
    # 2. Проверяем, принадлежит ли она текущему проекту
    if group.project_id != g.project.id:
        abort(403)

    try:
        db.session.delete(group)
        db.session.commit()
        flash(f'Группа OK "{group.name}" удалена.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при удалении группы: {e}', 'danger')

    return redirect(url_for('settings.social')) 

# --- ОТКЛЮЧЕНИЕ СОЦСЕТЕЙ (С УДАЛЕНИЕМ ДАННЫХ) ---

@settings_bp.route('/vk/disconnect')
@login_required
def vk_disconnect():
    if not g.project: return redirect(url_for('main.index'))
    
    success, msg = clear_vk_data(g.project.id)
    if success:
        flash('VK отключен, группы и связанные посты удалены.', 'success')
    else:
        flash(f'Ошибка очистки VK: {msg}', 'danger')
        
    return redirect(url_for('settings.social'))

@settings_bp.route('/ok/disconnect')
@login_required
def ok_disconnect():
    if not g.project: return redirect(url_for('main.index'))
    
    success, msg = clear_ok_data(g.project.id)
    if success:
        flash('OK отключен, группы и связанные посты удалены.', 'success')
    else:
        flash(f'Ошибка очистки OK: {msg}', 'danger')

    return redirect(url_for('settings.social'))

# --- УДАЛЕНИЕ ПРОЕКТА ---

@settings_bp.route('/project/delete/<int:project_id>')
@login_required
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    if project.user_id != current_user.id:
        abort(403)

    project_count = Project.query.filter_by(user_id=current_user.id).count()
    if project_count <= 1:
        flash('Нельзя удалить единственный проект! Создайте новый, затем удалите этот.', 'warning')
        return redirect(url_for('main.index'))
    
    # --- ИСПРАВЛЕНИЕ ТУТ ---
    # 1. Сохраняем имя проекта ЗАРАНЕЕ, пока он существует
    project_name = project.name 
    
    if 'project_id' in session and session['project_id'] == project.id:
        session.pop('project_id', None)
        
    # 2. Удаляем проект
    success, msg = delete_project_fully(project.id)
    
    if success:
        # 3. Используем сохраненную переменную project_name
        flash(f'Проект "{project_name}" удален.', 'success')
        
        remaining_project = Project.query.filter_by(user_id=current_user.id).first()
        if remaining_project:
            session['project_id'] = remaining_project.id
    else:
        flash(f'Ошибка удаления: {msg}', 'danger')
        
    return redirect(url_for('main.index')) 
    
# Обработка смены тарифа
@settings_bp.route('/update_tariff', methods=['POST'])
@login_required
def update_tariff():
    tariff_id = request.form.get('tariff_id')
    
    # 1. Получаем новый тариф
    new_tariff = db.session.get(Tariff, tariff_id)
    if not new_tariff or not new_tariff.is_active:
        flash('Тариф не найден или неактивен.', 'danger')
        return redirect(url_for('settings.profile'))

    # Если пытаемся выбрать тот же самый тариф
    if current_user.tariff_id == new_tariff.id:
        flash('Вы уже на этом тарифе.', 'info')
        return redirect(url_for('settings.profile'))

    # --- ПРОВЕРКА 1: Защита от частых переключений (Спам) ---
    # Можно оставить 24 часа, а можно уменьшить до 10 минут, раз теперь есть перерасчет.
    # Если перерасчет честный, жесткое ограничение в 24 часа можно убрать или ослабить.
    if current_user.last_tariff_change:
        time_diff = datetime.utcnow() - current_user.last_tariff_change
        if time_diff < timedelta(minutes=5): # Например, 5 минут кд
            flash('Подождите немного перед следующей сменой тарифа.', 'warning')
            return redirect(url_for('settings.profile'))

    # --- ПРОВЕРКА 2: Понижение (Downgrade) ---
    # Нельзя перейти, если занято больше ресурсов, чем дает новый тариф
    current_projects = Project.query.filter_by(user_id=current_user.id).count()
    if current_projects > new_tariff.max_projects:
        flash(f'Нельзя перейти на "{new_tariff.name}". У вас {current_projects} проектов, а лимит {new_tariff.max_projects}. Удалите лишние проекты.', 'danger')
        return redirect(url_for('settings.profile'))

    # --- ЛОГИКА ПЕРЕРАСЧЕТА (ВОЗВРАТ СРЕДСТВ) ---
    refund_amount = 0
    current_tariff = current_user.tariff_rel
    
    # Возвращаем деньги только если:
    # 1. Текущий тариф платный
    # 2. У него есть срок действия (не вечный)
    # 3. Срок еще не истек
    if current_tariff and current_tariff.price > 0 and current_user.tariff_expires_at:
        now = datetime.utcnow()
        if current_user.tariff_expires_at > now:
            # Считаем, сколько времени осталось
            time_left = current_user.tariff_expires_at - now
            total_duration = timedelta(days=current_tariff.days)
            
            # Процент неиспользованного времени (0.0 - 1.0)
            # Защита от деления на ноль, если days=0 (хотя выше проверка price>0)
            if total_duration.total_seconds() > 0:
                ratio = time_left.total_seconds() / total_duration.total_seconds()
                
                # Сумма к возврату (в копейках)
                refund_amount = int(current_tariff.price * ratio)

    # --- ПРОВЕРКА БАЛАНСА ---
    # У пользователя должно быть: (Текущий баланс + Возврат) >= Цена нового
    available_funds = current_user.balance + refund_amount
    
    if available_funds < new_tariff.price:
        needed = (new_tariff.price - available_funds) / 100
        flash(f'Недостаточно средств. С учетом возврата за старый тариф, вам не хватает {needed:.2f} ₽.', 'warning')
        return redirect(url_for('settings.profile'))

    # --- ПРОВЕДЕНИЕ ТРАНЗАКЦИЙ ---
    
    # 1. Возврат средств (если есть что возвращать)
    if refund_amount > 0:
        current_user.balance += refund_amount
        tx_refund = Transaction(
            user_id=current_user.id,
            amount=refund_amount,
            type='proration_refund',
            description=f'Перерасчет: возврат остатка за "{current_tariff.name}"'
        )
        db.session.add(tx_refund)

    # 2. Списание за новый тариф (если он платный)
    if new_tariff.price > 0:
        current_user.balance -= new_tariff.price
        tx_payment = Transaction(
            user_id=current_user.id,
            amount=-new_tariff.price, # Отрицательное число = списание
            type='tariff_payment',
            description=f'Оплата тарифа "{new_tariff.name}" ({new_tariff.days} дн.)'
        )
        db.session.add(tx_payment)
        msg = f'Тариф "{new_tariff.name}" подключен.'
    else:
        # Переход на бесплатный
        tx_switch = Transaction(
            user_id=current_user.id,
            amount=0,
            type='tariff_switch',
            description=f'Переход на бесплатный тариф "{new_tariff.name}"'
        )
        db.session.add(tx_switch)
        msg = f'Вы перешли на тариф "{new_tariff.name}".'

    # --- ОБНОВЛЕНИЕ ПОЛЬЗОВАТЕЛЯ ---
    current_user.tariff_id = new_tariff.id
    current_user.last_tariff_change = datetime.utcnow()
    
    # Устанавливаем НОВЫЙ срок (от текущего момента)
    if new_tariff.days > 0:
        current_user.tariff_expires_at = datetime.utcnow() + timedelta(days=new_tariff.days)
    else:
        current_user.tariff_expires_at = None # Бессрочный

    db.session.commit()
    
    # Если был возврат, добавим инфо в сообщение
    if refund_amount > 0:
        msg += f' (На баланс возвращено {(refund_amount/100):.2f} ₽ за неиспользованный период)'
        
    flash(msg, 'success')
    return redirect(url_for('settings.profile'))
    
@settings_bp.route('/profile')
@login_required
def profile():
    # 1. Тарифы для выбора
    tariffs = Tariff.query.filter_by(is_active=True).order_by(Tariff.price).all()
    
    # 2. История транзакций
    transactions = Transaction.query.filter_by(user_id=current_user.id)\
        .order_by(Transaction.created_at.desc()).limit(50).all()
    
    # 3. Текущее время (для расчета дней до конца тарифа)
    user_now = datetime.utcnow()
    
    return render_template('profile.html', 
                           tariffs=tariffs,
                           transactions=transactions,
                           user_now=user_now)    