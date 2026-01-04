# app/services.py
import os
import json
import logging
import requests
import base64
import hashlib
from datetime import datetime, timedelta

import vk_api
from vk_api.upload import VkUpload
from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError
from requests.exceptions import ConnectionError, Timeout, RequestException

from app import db, scheduler # <-- БЕЗ 'create_app'
from app.models import User, Post, SocialTokens, TgChannel, VkGroup

# Принудительно меняем адрес API по умолчанию (Monkey-Patch)
vk_api.vk_api.VkApi.DEFAULT_API_HOST = 'api.vk.ru'

# Получаем логгер Flask
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  VK: ЛОГИКА АВТО-ОБНОВЛЕНИЯ ТОКЕНА
# --------------------------------------------------------------------------

def _refresh_vk_token(user, tokens):
    """
    (Внутренняя функция)
    Пытается обновить Access Token, используя Refresh Token.
    Вызывается, если токен истек или скоро истечет.
    """
    logger.info(f"VK: Токен для пользователя {user.email} истекает. Начинаю обновление...")
    
    try:
        app_id = current_app.config.get('VK_APP_ID')
        
        # Генерируем новый state (как требует документация VK ID)
        state = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
        
        refresh_params = {
            'grant_type': 'refresh_token',
            'refresh_token': tokens.vk_refresh_token, # (Берем из БД)
            'client_id': app_id,
            'device_id': tokens.vk_device_id,     # (Берем из БД)
            'state': state
        }
        
        # Используем правильный URL для обмена (из документации)
        resp = requests.post(
            'https://id.vk.ru/oauth2/auth',
            data=refresh_params,
            timeout=10
        )
        resp.raise_for_status() # Вызовет ошибку, если VK ответил 4xx/5xx
        data = resp.json()
        
        new_access_token = data.get('access_token')
        new_refresh_token = data.get('refresh_token')
        new_expires_in = data.get('expires_in', 3600)

        if not new_access_token or not new_refresh_token:
            logger.error(f"VK Refresh Error: {data.get('error_description', 'Не получен токен')}")
            return None
            
        # Обновляем то, что у нас в БД
        tokens.vk_token = new_access_token
        tokens.vk_refresh_token = new_refresh_token
        tokens.vk_token_expires_at = datetime.utcnow() + timedelta(seconds=int(new_expires_in))
        db.session.commit()
        
        logger.info(f"VK: Токен для {user.email} успешно обновлен.")
        return new_access_token
        
    except RequestException as e:
        logger.error(f"VK Refresh FAILED: {e.response.text if e.response else e}")
        # Если refresh_token "умер" (отозван), пользователю придется 
        # авторизоваться заново в /settings/social
        return None
    except Exception as e:
        logger.error(f"VK Refresh FAILED (General): {e}", exc_info=True)
        return None

def get_valid_vk_session(user):
    """
    "Умная" функция: Проверяет токен. Если он истек, обновляет его.
    Возвращает готовый 'vk_session' или None, если ничего не вышло.
    """
    tokens = user.tokens
    
    if not all([tokens.vk_token, tokens.vk_refresh_token, 
                tokens.vk_device_id, tokens.vk_token_expires_at]):
        logger.error(f"VK: У пользователя {user.email} нет полных данных для refresh.")
        return None # (Нужно, чтобы юзер прошел авторизацию в /settings/social)

    # Проверяем, не истек ли токен (обновляем за 5 минут до "смерти")
    is_expired = (
        tokens.vk_token_expires_at <= (datetime.utcnow() + timedelta(minutes=5))
    )
    
    current_access_token = tokens.vk_token
    
    if is_expired:
        new_token = _refresh_vk_token(user, tokens)
        if new_token is None:
            return None # Обновление не удалось
        current_access_token = new_token
        
    # Возвращаем готовую сессию (УБИРАЕМ api_host, т.к. он в "патче")
    return vk_api.VkApi(
        token=current_access_token,
        api_version='5.199'
    )

# --------------------------------------------------------------------------
#  TELEGRAM: ЛОГИКА ОТПРАВКИ
# --------------------------------------------------------------------------

def TG_API(token, method):
    return f'https://api.telegram.org/bot{token}/{method}'

def tg_send_service(token, chat_id, text, media_paths, buttons_json):
    """
    Отправка в Telegram (Единый список файлов для правильного порядка).
    • 1 медиа -> sendPhoto / sendVideo + кнопки
    • >1 медиа -> sendMediaGroup (без кнопок)
    • 0 медиа -> sendMessage (+ кнопки)
    """
    
    buttons = json.loads(buttons_json) if buttons_json else []
    
    def make_kb():
        return {
            "inline_keyboard": [[
                {**({"text": b["text"], "callback_data": b["callback_data"]} if "callback_data" in b else
                   {"text": b["text"], "url": b["url"]})}
                for b in buttons
            ]]
        }

    # 1. Один файл (с кнопками)
    if len(media_paths) == 1:
        path = media_paths[0]
        # Определяем тип по расширению
        is_photo = path.lower().endswith(('.jpg', '.png', '.jpeg', '.webp'))
        field = 'photo' if is_photo else 'video'
        method = 'sendPhoto' if is_photo else 'sendVideo'
        
        try:
            with open(path, 'rb') as f:
                resp = requests.post(
                    TG_API(token, method),
                    data={
                        "chat_id": chat_id,
                        "caption": text,
                        "parse_mode": "HTML",
                        **({"reply_markup": json.dumps(make_kb())} if buttons else {})
                    },
                    files={field: f},
                    timeout=60
                )
            logger.info(f"Tg single media: {resp.text}")
            if resp.ok: 
                return resp.json()['result']['message_id'], None
            return None, resp.json().get('description')
        except Exception as e:
            logger.error(f"TG single media error: {e}")
            return None, str(e)

    # 2. Несколько файлов (БЕЗ кнопок, порядок сохраняется)
    if media_paths:
        media, files = [], {}
        try:
            for i, p in enumerate(media_paths):
                is_photo = p.lower().endswith(('.jpg', '.png', '.jpeg', '.webp'))
                typ = 'photo' if is_photo else 'video'
                name = os.path.basename(p)
                
                media.append({
                    "type": typ,
                    "media": f'attach://{name}',
                    # Текст прикрепляется только к первому элементу медиагруппы
                    **({"caption": text, "parse_mode": "HTML"} if i == 0 else {})
                })
                files[name] = open(p, 'rb')
                
            resp = requests.post(TG_API(token, 'sendMediaGroup'),
                                 data={"chat_id": chat_id, "media": json.dumps(media)},
                                 files=files,
                                 timeout=120)
            
            # Обязательно закрываем файлы
            for fh in files.values(): fh.close()
            
            logger.info(f"Tg album: {resp.text}")
            
            if resp.ok:
                # Возвращаем ID первого сообщения в группе
                return resp.json()['result'][0]['message_id'], None
            return None, resp.json().get('description')
        except Exception as e:
            logger.error(f"TG album error: {e}")
            for fh in files.values(): fh.close()
            return None, str(e)
        
    # 3. Только текст (с кнопками)
    try:
        resp = requests.post(TG_API(token, 'sendMessage'),
                             json={
                                 "chat_id": chat_id,
                                 "text": text or ".",
                                 "parse_mode": "HTML",
                                 **({"reply_markup": json.dumps(make_kb())} if buttons else {})
                             },
                             timeout=30)
        logger.info(f"Tg text: {resp.text}")
        if resp.ok: 
            return resp.json()['result']['message_id'], None
        return None, resp.json().get('description')
    except Exception as e:
        logger.error(f"TG text error: {e}")
        return None, str(e)

def fetch_tg_channels(token, user_id):
    """
    (Старая функция, пока не используется, но нужна для импорта)
    Получает список каналов/чатов, где бот является админом.
    """
    try:
        me_resp = requests.get(TG_API(token, 'getMe'), timeout=10)
        if not me_resp.ok:
            return None, "Неверный токен (getMe)"
        bot_id = me_resp.json()['result']['id']
        
        updates_resp = requests.get(TG_API(token, 'getUpdates'), params={"limit": 50}, timeout=10)
        if not updates_resp.ok:
            return None, "Не удалось получить обновления"
            
        chats = {} # chat_id -> name
        for update in updates_resp.json().get('result', []):
            chat = update.get('message', {}).get('chat') or \
                   update.get('my_chat_member', {}).get('chat')
            if not chat: continue
            if chat['type'] in ['channel', 'supergroup']:
                try:
                    admin_resp = requests.get(TG_API(token, 'getChatMember'), 
                                              params={'chat_id': chat['id'], 'user_id': bot_id},
                                              timeout=5)
                    if admin_resp.ok and admin_resp.json()['result']['status'] in ['administrator', 'creator']:
                         chats[chat['id']] = chat.get('title', 'Без имени')
                except Exception:
                    pass 

        if not chats:
            return None, "Не найдено каналов, где бот является админом."
        
        TgChannel.query.filter_by(user_id=user_id).delete()
        for chat_id, name in chats.items():
            db.session.add(TgChannel(user_id=user_id, name=name, chat_id=str(chat_id)))
        db.session.commit()
        return f"Найдено и сохранено каналов: {len(chats)}", None
    except Exception as e:
        logger.error(f"TG fetch channels error: {e}")
        return None, str(e)
# --- ^ --- КОНЕЦ ФУНКЦИИ --- ^ ---


# --------------------------------------------------------------------------
#  VK: ЛОГИКА ОТПРАВКИ И СИНХРОНИЗАЦИИ
# --------------------------------------------------------------------------

def vk_send_service(user, group_id, text, media_paths, 
                    layout='grid', schedule_at_utc=None):
    
    vk_session = get_valid_vk_session(user)
    if vk_session is None:
        return None, "Не удалось получить/обновить VK токен."

    try:
        vk_upload  = VkUpload(vk_session)
        vk_api_raw = vk_session.get_api()

        attach = []
        
        # Обрабатываем файлы В ПОРЯДКЕ ОЧЕРЕДИ
        for p in media_paths:
            if p.lower().endswith(('.jpg', '.png', '.jpeg')):
                # Это ФОТО
                try:
                    a = vk_upload.photo_wall(p, group_id=abs(int(group_id)))[0]
                    attach.append(f"photo{a['owner_id']}_{a['id']}")
                except Exception as e:
                    logger.error(f"VK Photo Upload Error: {e}")
            elif p.lower().endswith(('.mp4', '.mov', '.avi')):
                # Это ВИДЕО
                try:
                    v = vk_upload.video(video_file=p, name=text[:50], 
                                        group_id=abs(int(group_id)))
                    attach.append(f"video{v['owner_id']}_{v['video_id']}")
                except Exception as e:
                    logger.error(f"VK Video Upload Error: {e}")
        
        wall_params = dict(
            owner_id=-abs(int(group_id)),
            from_group=1,
            message=text,
            attachments=",".join(attach)
        )
        
        # (Карусель работает только если >1 вложения и нет видео)
        has_video = any('video' in a for a in attach)
        if layout == 'grid' and len(attach) > 1 and not has_video:
            wall_params['primary_attachments_mode'] = 'grid'
        
        if schedule_at_utc:
            wall_params['publish_date'] = int(schedule_at_utc.timestamp())

        post = vk_api_raw.wall.post(**wall_params)
        return post['post_id'], None

    except Exception as e:
        logger.error(f"VK send error: {e}", exc_info=True)
        if isinstance(e, vk_api.ApiError) and e.code == 15:
             return None, "Ошибка [15]: VK API отказал в доступе."
        return None, str(e)


def fetch_vk_groups(user, project_id):
    """
    Синхронизация групп VK для конкретного ПРОЕКТА.
    """
    # 1. Получаем сессию
    vk_session = get_valid_vk_session(user)
    if vk_session is None:
        return None, "Не удалось получить/обновить VK токен."
        
    try:
        vk_api_raw = vk_session.get_api()
        
        try:
            groups_data = vk_api_raw.groups.get(extended=1, filter='admin, editor')
            api_groups = groups_data.get('items', [])
            api_group_count = groups_data.get('count', 0)
        except vk_api.ApiError as e:
            return None, f"Ошибка VK API: {e}"

        if api_group_count == 0:
            return None, "Не найдено администрируемых групп."
        
        # 3. Получаем группы ТОЛЬКО ТЕКУЩЕГО ПРОЕКТА
        db_groups_list = VkGroup.query.filter_by(
            user_id=user.id, 
            project_id=project_id
        ).all()
        
        db_groups_map = {g.group_id: g for g in db_groups_list}
        processed_group_ids = set()
        new_groups_added = 0

        # 4. Синхронизируем
        for api_group in api_groups:
            group_id = api_group['id']
            group_name = api_group['name']
            
            if group_id not in db_groups_map:
                # Добавляем с привязкой к проекту!
                new_g = VkGroup(
                    user_id=user.id,
                    project_id=project_id, # <--- ВАЖНО
                    name=group_name,
                    group_id=group_id
                )
                db.session.add(new_g)
                new_groups_added += 1
            else:
                # Обновляем имя
                db_groups_map[group_id].name = group_name
            
            processed_group_ids.add(group_id)

        # 5. Удаляем лишние из этого проекта
        groups_deleted = 0
        groups_kept = 0
        
        for db_group in db_groups_list:
            if db_group.group_id not in processed_group_ids:
                try:
                    db.session.delete(db_group)
                    db.session.flush() 
                    groups_deleted += 1
                except IntegrityError:
                    db.session.rollback() 
                    groups_kept += 1
        
        db.session.commit()
        msg = (
            f"Синхронизировано: {api_group_count} (новых: {new_groups_added}). "
            f"Удалено: {groups_deleted}. "
        )
        if groups_kept:
            msg += f" (Оставлено {groups_kept} из-за связей с постами)"
            
        return msg, None

    except Exception as e:
        db.session.rollback() 
        logger.error(f"VK fetch groups error: {e}", exc_info=True)
        return None, str(e)


# --------------------------------------------------------------------------
#  INSTAGRAM: ЛОГИКА ОТПРАВКИ
# --------------------------------------------------------------------------

def ig_get_public_url(filename):
    """
    Возвращает полный публичный URL для файла в /static/uploads/
    """
    base_url = current_app.config.get('APP_URL') 
    if not base_url:
        try:
            base_url = url_for('main.index', _external=True)
        except Exception:
            # (Работаем вне контекста)
            return f"/static/uploads/{filename}" # (Неполный URL, но лучше, чем ничего)
        
    return f"{base_url.rstrip('/')}/static/uploads/{filename}"


def ig_send_service(user, image_path, caption):
    """
    Отправляет 1 фото в Instagram.
    """
    tokens = user.tokens
    IG_USER_ID = tokens.ig_user_id
    IG_TOKEN = tokens.ig_page_token 
    
    if not IG_USER_ID or not IG_TOKEN:
        return "Instagram не настроен (нет ID или Токена)."

    filename = os.path.basename(image_path)
    public_url = ig_get_public_url(filename)

    upload_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media",
        params={
            "image_url": public_url,
            "caption": caption[:2200],
            "access_token": IG_TOKEN
        },
        timeout=30
    )
    
    try:
        upload = upload_resp.json()
    except requests.JSONDecodeError:
        return f"IG upload: Ошибка ответа API (не JSON): {upload_resp.text[:100]}"
        
    logger.info(f"IG upload resp: {upload}")
    if "error" in upload:
        return f"IG upload: {upload['error'].get('message', str(upload))}"
    if "id" not in upload:
        return f"IG upload: Не получен creation_id."

    publish_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media_publish",
        params={"creation_id": upload["id"], "access_token": IG_TOKEN},
        timeout=30
    )
    
    try:
        publish = publish_resp.json()
    except requests.JSONDecodeError:
        return f"IG publish: Ошибка ответа API (не JSON): {publish_resp.text[:100]}"

    logger.info(f"IG publish resp: {publish}")
    if "error" in publish:
        return f"IG publish: {publish['error'].get('message', str(publish))}"

    return None   # Успех!

# --------------------------------------------------------------------------
#  ГЛАВНАЯ ФОНОВАЯ ЗАДАЧА (APScheduler)
# --------------------------------------------------------------------------

def publish_post_task(post_id):
    """
    Главная задача, которую вызывает планировщик.
    """

    from app import create_app 
    app = create_app() 
    
    with app.app_context():
        logger.info(f"[Task: {post_id}] Начинаю публикацию...")
        
        post = Post.query.get(post_id)
        if not post:
            logger.error(f"[Task: {post_id}] Пост не найден.")
            return

        user = User.query.get(post.user_id)
        if not (post and user and user.tokens):
            logger.error(f"[Task: {post_id}] Не найдены данные (пост, юзер или токены).")
            post.status = 'failed'
            post.error_message = 'Не найдены данные пользователя или токенов.'
            db.session.commit()
            return
            
        tokens = user.tokens 
        post.status = 'publishing'
        post.error_message = None 
        db.session.commit()

        upload_folder = app.config['UPLOAD_FOLDER']
        media_files = post.media_files if post.media_files else []
        full_paths = [os.path.join(upload_folder, f) for f in media_files]
        
        images = [p for p in full_paths if p.lower().endswith(('.jpg', '.png', '.jpeg'))]
        videos = [p for p in full_paths if p.lower().endswith(('.mp4', '.mov'))] 
        
        logger.info(f"[Task: {post_id}] Processing {len(media_files)} files: {media_files}")

        platform_info = post.platform_info or {}
        errors = []
        
        buttons_list = platform_info.get('buttons', [])
        buttons_json_str = json.dumps(buttons_list)

        # --- 1. Публикация в Telegram ---
        if post.publish_to_tg and post.tg_channel_id:
            logger.info(f"[Task: {post_id}] Публикую в TG...")
            if not tokens.tg_token:
                errors.append("TG: Токен не найден.")
            else:
                channel = TgChannel.query.get(post.tg_channel_id)
                if channel:
                    msg_id, err = tg_send_service(tokens.tg_token, channel.chat_id, 
                                                  post.text, full_paths, 
                                                  buttons_json=buttons_json_str)                
                    if err:
                        errors.append(f"TG: {err}")
                    else:
                        platform_info['tg_msg_id'] = msg_id
                else:
                    errors.append("TG: Выбранный канал не найден.")

        # --- 2. Публикация в VK ---
        # (Этот блок теперь ПУСТОЙ, т.к. VK обрабатывается 
        # в 'routes_main.py' при создании поста)
        if post.publish_to_vk and post.vk_group_id:
            logger.info(f"[Task: {post_id}] VK-пост уже обработан в routes_main. Пропускаю.")
            pass # (Просто пропускаем)        
        
        
        # if post.publish_to_vk and post.vk_group_id:
            # logger.info(f"[Task: {post_id}] Публикую в VK...")
            # vk_group = VkGroup.query.get(post.vk_group_id)
            # if not vk_group:
                 # errors.append("VK: Выбранная группа не найдена.")
            # else:
                # vk_text = post.text_vk or post.text
                # post_id_vk, err = vk_send_service(
                    # user, 
                    # vk_group.group_id,
                    # vk_text,
                    # images,
                    # videos,
                    # layout=post.vk_layout, 
                    # schedule_at_utc=None
                # )
                # if err:
                    # errors.append(f"VK: {err}")
                # else:
                    # platform_info['vk_post_id'] = post_id_vk

        # --- 3. Публикация в IG ---
        if post.publish_to_ig:
            logger.info(f"[Task: {post_id}] Публикую в IG...")
            if not images:
                errors.append("IG: Для публикации в Instagram нужно хотя бы 1 фото.")
            else:
                try:
                    first_image_path = images[0] 
                    err = ig_send_service(user, first_image_path, post.text_vk)
                    if err:
                        errors.append(f"IG: {err}")
                except Exception as e:
                    errors.append(f"IG: {str(e)}")

        # --- Завершение ---
        if errors:
            post.status = 'failed'
            post.error_message = " | ".join(errors)
            logger.error(f"[Task: {post_id}] Публикация не удалась: {post.error_message}")
        else:
            post.status = 'published'
            post.published_at = datetime.utcnow()
            logger.info(f"[Task: {post_id}] Публикация прошла успешно.")

        post.platform_info = platform_info
        db.session.commit()

# --------------------------------------------------------------------------
#  ЛОГИКА УДАЛЕНИЯ ПОСТОВ
# --------------------------------------------------------------------------

def tg_delete_service(token, chat_id, msg_id):
    """
    Выполняет API-запрос на удаление сообщения в Telegram.
    """
    try:
        resp = requests.post(
            TG_API(token, 'deleteMessage'),
            json={"chat_id": chat_id, "message_id": msg_id},
            timeout=10
        )
        if resp.ok:
            logger.info(f"TG: Успешно удален msg_id {msg_id} из chat_id {chat_id}")
            return True, None
        logger.warning(f"TG: Не удалось удалить msg_id {msg_id}: {resp.text}")
        return False, resp.json().get('description')
    except Exception as e:
        logger.error(f"TG delete error: {e}")
        return False, str(e)

def vk_delete_service(user, owner_id, post_id):
    """
    Выполняет API-запрос на удаление поста VK (с авто-обновлением токена).
    """
    # 1. Получаем "свежую" сессию
    vk_session = get_valid_vk_session(user)
    if vk_session is None:
        return False, "Не удалось получить/обновить VK токен."
	   
    try:
        # 2. Выполняем удаление
        vk_api_raw = vk_session.get_api()
        # owner_id (ID группы) для VK API должен быть отрицательным
        vk_api_raw.wall.delete(owner_id=-abs(int(owner_id)), post_id=post_id)
        
        logger.info(f"VK: Успешно удален post_id {post_id} из owner_id {owner_id}")
        return True, None
        
    except vk_api.ApiError as e:
        logger.error(f"VK delete error: {e}")
        return False, str(e)
    except Exception as e:
        logger.error(f"VK delete error (general): {e}", exc_info=True)
        return False, str(e)