# app/services.py
import os
import json
import logging
import requests
import base64
import hashlib
import fcntl
from datetime import datetime, timedelta
import mimetypes
import vk_api
from vk_api.upload import VkUpload
from flask import current_app, url_for, jsonify
from sqlalchemy.exc import IntegrityError
from requests.exceptions import ConnectionError, Timeout, RequestException

from app import db, scheduler, create_app
from app.email import send_email 
from app.models import User, Post, SocialTokens, TgChannel, VkGroup, OkGroup, MaxChat, RssSource, Project, Tariff, Transaction, AppSettings

# Принудительно меняем адрес API VK по умолчанию
vk_api.vk_api.VkApi.DEFAULT_API_HOST = 'api.vk.ru'

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
#  VK: ЛОГИКА АВТО-ОБНОВЛЕНИЯ ТОКЕНА
# --------------------------------------------------------------------------

def _refresh_vk_token(tokens_obj):
    """
    (Внутренняя функция) Обновляет Access Token через Refresh Token.
    """
    logger.info(f"VK: Токен для проекта {tokens_obj.project_id} истекает. Начинаю обновление...")
    try:
        app_id = current_app.config.get('VK_APP_ID')
        state = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
        
        refresh_params = {
            'grant_type': 'refresh_token',
            'refresh_token': tokens_obj.vk_refresh_token, 
            'client_id': app_id,
            'device_id': tokens_obj.vk_device_id,     
            'state': state
        }
        
        resp = requests.post('https://id.vk.ru/oauth2/auth', data=refresh_params, timeout=10)
        resp.raise_for_status() 
        data = resp.json()
        
        new_access_token = data.get('access_token')
        new_refresh_token = data.get('refresh_token')
        new_expires_in = data.get('expires_in', 3600)

        if not new_access_token or not new_refresh_token:
            logger.error(f"VK Refresh Error: {data.get('error_description', 'Не получен токен')}")
            return None
            
        tokens_obj.vk_token = new_access_token
        tokens_obj.vk_refresh_token = new_refresh_token
        tokens_obj.vk_token_expires_at = datetime.utcnow() + timedelta(seconds=int(new_expires_in))
        db.session.commit()
        
        logger.info(f"VK: Токен успешно обновлен.")
        return new_access_token
        
    except Exception as e:
        logger.error(f"VK Refresh FAILED: {e}", exc_info=True)
        return None

def get_valid_vk_session(tokens_obj):
    """
    Возвращает валидную сессию VK, обновляя токен при необходимости.
    """
    if not tokens_obj: return None
    
    if not all([tokens_obj.vk_token, tokens_obj.vk_refresh_token, 
                tokens_obj.vk_device_id, tokens_obj.vk_token_expires_at]):
        return None    

    is_expired = (tokens_obj.vk_token_expires_at <= (datetime.utcnow() + timedelta(minutes=5)))
    current_access_token = tokens_obj.vk_token
    
    if is_expired:
        new_token = _refresh_vk_token(tokens_obj) 
        if new_token is None:
            return None
        current_access_token = new_token
        
    return vk_api.VkApi(token=current_access_token, api_version='5.199')


# --------------------------------------------------------------------------
#  TELEGRAM
# --------------------------------------------------------------------------

def TG_API(token, method):
    return f'https://api.telegram.org/bot{token}/{method}'

def tg_send_service(token, chat_id, text, media_paths, buttons_json):
    
    # Telegram API лимиты: 1024 символа с медиа, 4096 без медиа.
    limit = 1024 if media_paths else 4096
    
    if text and len(text) > limit:
        # Обрезаем текст и добавляем троеточие, чтобы влезть в лимит
        text = text[:limit - 3] + "..."
    # --- КОНЕЦ ВСТАВКИ ---    
    
    buttons = json.loads(buttons_json) if buttons_json else []
    
    def make_kb():
        return {
            "inline_keyboard": [[
                {**({"text": b["text"], "callback_data": b["callback_data"]} if "callback_data" in b else
                   {"text": b["text"], "url": b["url"]})}
                for b in buttons
            ]]
        }

    # 1. Один файл
    if len(media_paths) == 1:
        path = media_paths[0]
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
            if resp.ok: 
                return resp.json()['result']['message_id'], None
            return None, resp.json().get('description')
        except Exception as e:
            return None, str(e)

    # 2. Альбом
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
                    **({"caption": text, "parse_mode": "HTML"} if i == 0 else {})
                })
                files[name] = open(p, 'rb')
                
            resp = requests.post(TG_API(token, 'sendMediaGroup'),
                                 data={"chat_id": chat_id, "media": json.dumps(media)},
                                 files=files, timeout=120)
            for fh in files.values(): fh.close()
            
            if resp.ok:
                return resp.json()['result'][0]['message_id'], None
            return None, resp.json().get('description')
        except Exception as e:
            for fh in files.values(): fh.close()
            return None, str(e)
        
    # 3. Текст
    try:
        resp = requests.post(TG_API(token, 'sendMessage'),
                             json={
                                 "chat_id": chat_id,
                                 "text": text or ".",
                                 "parse_mode": "HTML",
                                 **({"reply_markup": json.dumps(make_kb())} if buttons else {})
                             }, timeout=30)
        logger.info(f"Tg text: {resp.text}")
        if resp.ok: 
            return resp.json()['result']['message_id'], None
        return None, resp.json().get('description')
    except Exception as e:
        logger.error(f"TG text error: {e}")
        return None, str(e)

def tg_delete_service(token, chat_id, msg_id):
    try:
        resp = requests.post(TG_API(token, 'deleteMessage'),
            json={"chat_id": chat_id, "message_id": msg_id}, timeout=10)
        if resp.ok: return True, None
        return False, resp.json().get('description')
    except Exception as e:
        return False, str(e)

def fetch_tg_channels(token, user_id):
    """Получает список каналов, где бот является админом."""
    try:
        me_resp = requests.get(TG_API(token, 'getMe'), timeout=10)
        if not me_resp.ok:
            return None, "Неверный токен (getMe)"
        bot_id = me_resp.json()['result']['id']
        
        updates_resp = requests.get(TG_API(token, 'getUpdates'), params={"limit": 50}, timeout=10)
        
        chats = {} 
        if updates_resp.ok:
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
                    except Exception: pass 

        # Для сохранения нужно знать project_id, но эта функция вызывается из routes_settings, 
        # где логика сохранения реализована отдельно или передается.
        # В старой версии она сохраняла в БД. Для совместимости с новой архитектурой 
        # лучше возвращать словарь, а сохранять в роуте.
        # Но если мы хотим сохранить "как было":
        
        # ВНИМАНИЕ: Здесь мы не знаем project_id, поэтому просто вернем найденное сообщение.
        # Реальное сохранение лучше делать через webhook или ручное добавление,
        # так как getUpdates ненадежен для старых каналов.
        if not chats:
            return "Не найдено новых каналов в getUpdates.", None
            
        return f"Найдено в обновлениях: {len(chats)} шт. (Добавьте их вручную по ID)", None
    except Exception as e:
        return None, str(e)


# --------------------------------------------------------------------------
#  VKONTAKTE
# --------------------------------------------------------------------------

def vk_send_service(project_tokens, group_id, text, media_paths, 
                    layout='grid', schedule_at_utc=None):
    
    vk_session = get_valid_vk_session(project_tokens)
    if vk_session is None:
        return None, "Не удалось получить/обновить VK токен."

    try:
        vk_upload  = VkUpload(vk_session)
        vk_api_raw = vk_session.get_api()
        attach = []
        
        for p in media_paths:
            if p.lower().endswith(('.jpg', '.png', '.jpeg')):
                try:
                    a = vk_upload.photo_wall(p, group_id=abs(int(group_id)))[0]
                    attach.append(f"photo{a['owner_id']}_{a['id']}")
                except Exception as e:
                    logger.error(f"VK Photo Upload Error: {e}")
            elif p.lower().endswith(('.mp4', '.mov', '.avi')):
                try:
                    v = vk_upload.video(video_file=p, name=text[:50], group_id=abs(int(group_id)))
                    attach.append(f"video{v['owner_id']}_{v['video_id']}")
                except Exception as e:
                    logger.error(f"VK Video Upload Error: {e}")
        
        wall_params = dict(
            owner_id=-abs(int(group_id)),
            from_group=1,
            message=text,
            attachments=",".join(attach)
        )
        
        has_video = any('video' in a for a in attach)
        if layout == 'grid' and len(attach) > 1 and not has_video:
            wall_params['primary_attachments_mode'] = 'grid'
        
        if schedule_at_utc:
            wall_params['publish_date'] = int(schedule_at_utc.timestamp())

        post = vk_api_raw.wall.post(**wall_params)
        return post['post_id'], None
    except Exception as e:
        logger.error(f"VK send error: {e}")
        return None, str(e)

def vk_delete_service(tokens_obj, owner_id, post_id):
    vk_session = get_valid_vk_session(tokens_obj)
    if vk_session is None: return False, "Ошибка токена VK"
    try:
        vk_session.get_api().wall.delete(owner_id=-abs(int(owner_id)), post_id=post_id)
        return True, None
    except Exception as e:
        return False, str(e)

def fetch_vk_groups(user, project_id):
    """Синхронизация групп VK."""
    tokens = SocialTokens.query.filter_by(project_id=project_id).first()
    vk_session = get_valid_vk_session(tokens)
    if vk_session is None: return None, "Ошибка токена VK"
        
    try:
        vk_api_raw = vk_session.get_api()
        groups_data = vk_api_raw.groups.get(extended=1, filter='admin, editor')
        api_groups = groups_data.get('items', [])
        
        if not api_groups: return None, "Не найдено групп."
        
        db_groups_list = VkGroup.query.filter_by(project_id=project_id).all()
        db_groups_map = {g.group_id: g for g in db_groups_list}
        processed = set()
        new_added = 0

        for ag in api_groups:
            gid = ag['id']
            name = ag['name']
            if gid not in db_groups_map:
                db.session.add(VkGroup(user_id=user.id, project_id=project_id, name=name, group_id=gid))
                new_added += 1
            else:
                db_groups_map[gid].name = name
            processed.add(gid)

        # Удаление старых
        deleted = 0
        for dbg in db_groups_list:
            if dbg.group_id not in processed:
                try:
                    db.session.delete(dbg)
                    db.session.flush()
                    deleted += 1
                except IntegrityError:
                    db.session.rollback()
        
        db.session.commit()
        return f"Синхронизировано: {len(api_groups)} (новых: {new_added}, удалено: {deleted})", None
    except Exception as e:
        db.session.rollback()
        return None, str(e)

# --------------------------------------------------------------------------
#  ODNOKLASSNIKI (OK) - НОВАЯ ЛОГИКА
# --------------------------------------------------------------------------

def _refresh_ok_token(tokens_obj):
    """Обновляет токен Одноклассников."""
    logger.info(f"OK: Обновление токена для проекта {tokens_obj.project_id}...")
    try:
        refresh_token = tokens_obj.ok_refresh_token
        client_id = current_app.config.get('OK_CLIENT_ID')
        client_secret = current_app.config.get('OK_CLIENT_SECRET')
        
        if not all([refresh_token, client_id, client_secret]):
            logger.error("OK Refresh: нет refresh_token или ключей приложения.")
            return False

        resp = requests.post('https://api.ok.ru/oauth/token.do', data={
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'client_secret': client_secret
        }, timeout=15)
        
        data = resp.json()
        new_access = data.get('access_token')
        # OK может вернуть новый refresh_token, а может оставить старый
        new_refresh = data.get('refresh_token') 
        
        if not new_access:
            logger.error(f"OK Refresh Failed: {data}")
            return False
            
        tokens_obj.ok_token = new_access
        if new_refresh:
            tokens_obj.ok_refresh_token = new_refresh
            
        db.session.commit()
        logger.info("OK: Токен успешно обновлен.")
        return True
        
    except Exception as e:
        logger.error(f"OK Refresh Error: {e}", exc_info=True)
        return False

def _ok_make_request(tokens, method, params_dict):
    """
    Вспомогательная функция для выполнения запроса с подписью.
    Возвращает JSON ответа или вызывает ошибку.
    """
    token = tokens.ok_token
    pub_key = tokens.ok_app_pub_key or current_app.config.get('OK_APP_PUB_KEY')
    secret_key = tokens.ok_app_secret_key or current_app.config.get('OK_CLIENT_SECRET')
    
    # Базовые параметры
    req_params = params_dict.copy()
    req_params['application_key'] = pub_key
    req_params['format'] = 'json'
    req_params['method'] = method
    
    # Подпись
    sig_str = "".join([f"{k}={req_params[k]}" for k in sorted(req_params.keys())])
    session_secret = hashlib.md5((token + secret_key).encode("utf-8")).hexdigest()
    sig_str += session_secret
    signature = hashlib.md5(sig_str.encode("utf-8")).hexdigest()
    
    req_params['access_token'] = token
    req_params['sig'] = signature
    
    resp = requests.post("https://api.ok.ru/fb.do", data=req_params, timeout=60)
    return resp.json()

def _ok_upload_images(tokens, group_id, image_paths):
    """
    Загружает изображения на сервер OK с явным указанием MIME-типа.
    """
    if not image_paths: return []
    
    try:
        # 1. Получаем URL для загрузки
        # Обязательно передаем gid, чтобы фото привязались к группе
        data = _ok_make_request(tokens, "photosV2.getUploadUrl", {
            "gid": str(group_id), 
            "count": len(image_paths)
        })
        
        # Рефреш токена при ошибке 102
        if isinstance(data, dict) and data.get("error_code") == 102:
            if _refresh_ok_token(tokens):
                data = _ok_make_request(tokens, "photosV2.getUploadUrl", {
                    "gid": str(group_id), "count": len(image_paths)
                })
            else:
                return []
                
        upload_url = data.get('upload_url')
        if not upload_url:
            logger.error(f"OK Photo: Не получен upload_url. Ответ: {data}")
            return []
            
        # 2. Формируем файлы с указанием MIME-типа
        # Это критически важно для OK API, иначе токен будет невалидным
        files = {}
        opened_files = []
        
        for i, path in enumerate(image_paths):
            f = open(path, 'rb')
            opened_files.append(f)
            
            filename = os.path.basename(path)
            # Определяем тип файла (image/jpeg, image/png и т.д.)
            mime_type, _ = mimetypes.guess_type(path)
            if not mime_type:
                mime_type = 'image/jpeg' # Фолбэк
            
            # Структура: 'ключ': ('имя_файла', поток, 'mime/type')
            files[f"pic{i+1}"] = (filename, f, mime_type)
            
        # 3. Отправляем
        # Таймаут побольше, так как загрузка медиа может быть долгой
        up_resp = requests.post(upload_url, files=files, timeout=120)
        
        # Закрываем файлы
        for f in opened_files: f.close()
        
        res_json = up_resp.json()
        logger.info(f"OK Upload Response: {res_json}") 

        # 4. Собираем токены (ИСПРАВЛЕННАЯ ЛОГИКА)
        if "photos" not in res_json:
            logger.error(f"OK: Ошибка загрузки фото (нет ключа photos): {res_json}")
            return []

        photos_map = res_json.get('photos', {})
        result_list = []
        
        for key, value in photos_map.items():
            # В логах видно структуру: { "key": {"token": "REAL_TOKEN"} }
            if isinstance(value, dict) and "token" in value:
                result_list.append({"id": value["token"]})
            else:
                # На случай, если формат вернется к старому (просто ключ-значение)
                logger.warning(f"OK: Нестандартный ответ фото: {value}")
                # Пробуем взять сам ключ, если токена нет (фолбэк)
                result_list.append({"id": key})
        
        return result_list
        
    except Exception as e:
        logger.error(f"OK Photo Upload Error: {e}", exc_info=True)
        return []

def _ok_upload_video(tokens, group_id, video_path):
    """
    Загружает ОДНО видео. Возвращает объект {"id": video_id} или None.
    """
    try:
        file_size = os.path.getsize(video_path)
        file_name = os.path.basename(video_path)
        
        # 1. Получаем URL
        data = _ok_make_request(tokens, "video.getUploadUrl", {
            "gid": group_id,
            "file_name": file_name,
            "file_size": file_size
        })
        
        # Рефреш токена, если нужно
        if isinstance(data, dict) and data.get("error_code") == 102:
            if _refresh_ok_token(tokens):
                data = _ok_make_request(tokens, "video.getUploadUrl", {
                    "gid": group_id, "file_name": file_name, "file_size": file_size
                })
            else: return None

        upload_url = data.get('upload_url')
        video_id = data.get('video_id')
        
        if not upload_url:
            logger.error(f"OK Video: Не получен upload_url. {data}")
            return None
            
        # 2. Загружаем файл
        with open(video_path, 'rb') as f:
            requests.post(upload_url, data=f, timeout=300) # data=f для потоковой загрузки
            
        # 3. Возвращаем ID
        # (В OK видео становится доступным не сразу, но ID мы получаем сразу)
        return {"id": str(video_id)}
        
    except Exception as e:
        logger.error(f"OK Video Upload Error: {e}")
        return None

def ok_send_service(project_tokens, group_id, text, media_paths):
    """
    Отправка поста в OK с Фото и Видео.
    """
    if not project_tokens.ok_token:
        return None, "Нет токена OK"

    # 1. Сортируем файлы
    images = [p for p in media_paths if p.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
    videos = [p for p in media_paths if p.lower().endswith(('.mp4', '.mov', '.avi'))]

    media_json = []

    # 2. Добавляем Текст
    if text:
        media_json.append({"type": "text", "text": text})

    # 3. Загружаем и добавляем Фото
    if images:
        try:
            photo_list = _ok_upload_images(project_tokens, group_id, images)
            if photo_list:
                media_json.append({"type": "photo", "list": photo_list})
        except Exception as e:
            logger.error(f"OK: Ошибка при загрузке фото: {e}")

    # 4. Загружаем и добавляем Видео
    if videos:
        movie_list = []
        for v_path in videos:
            v_res = _ok_upload_video(project_tokens, group_id, v_path)
            if v_res:
                movie_list.append(v_res)
        
        if movie_list:
             media_json.append({"type": "movie", "list": movie_list})

    if not media_json:
         return None, "Пустой пост (нет текста и медиа не загрузились)"
         
    # 5. Формируем финальный запрос
    attachment_str = json.dumps({"media": media_json}, separators=(',', ':'), ensure_ascii=False)
    
    params = {
        "gid": str(group_id),
        "type": "GROUP_THEME",
        "attachment": attachment_str
    }
    
    # Попытка отправки с ретраем на 102 ошибку
    try:
        data = _ok_make_request(project_tokens, "mediatopic.post", params)
    except Exception as e:
        return None, str(e)
        
    # Проверка на протухший токен (если вдруг он протух именно в момент поста)
    if isinstance(data, dict) and data.get("error_code") == 102:
        logger.warning("OK: Error 102 при отправке поста. Рефреш...")
        if _refresh_ok_token(project_tokens):
            # Если обновили, нужно заново загружать фото/видео? 
            # Нет, ID загруженных фото живут некоторое время. Пробуем повторить только пост.
            try:
                data = _ok_make_request(project_tokens, "mediatopic.post", params)
            except Exception as e:
                return None, f"Retry failed: {e}"
        else:
            return None, "OK: Токен истек."

    if isinstance(data, dict) and "error_code" in data:
        return None, f"OK Error {data.get('error_code')}: {data.get('error_msg')}"
        
    if isinstance(data, dict): return data.get("id"), None
    return str(data), None

def fetch_ok_groups(project_id):
    tokens = SocialTokens.query.filter_by(project_id=project_id).first()
    if not tokens or not tokens.ok_token: return None, "Нет токена OK"

    # Попытка 1: Получаем ID групп
    try:
        data = _ok_make_request(tokens, "group.getUserGroupsV2", {"count": "100"})
    except Exception as e: return None, str(e)

    # Рефреш при 102
    if isinstance(data, dict) and data.get("error_code") == 102:
        if _refresh_ok_token(tokens):
            data = _ok_make_request(tokens, "group.getUserGroupsV2", {"count": "100"})
        else:
            return None, "Token expired"

    if "error_code" in data: return None, f"OK API Error: {data.get('error_msg')}"
    
    raw_groups = data.get('groups', [])
    target_gids = []
    for g in raw_groups:
        role = str(g.get('role', '')).upper()
        status = str(g.get('status', '')).upper()
        if role in ['ADMIN', 'MODERATOR', 'SUPER_MODERATOR', 'EDITOR'] or status == 'ADMIN':
            target_gids.append(g.get('groupId'))
            
    if not target_gids: return None, "Нет администрируемых групп."

    # Попытка получения имен (group.getInfo)
    gids_str = ",".join(target_gids[:50])
    data_info = _ok_make_request(tokens, "group.getInfo", {
        "uids": gids_str, "fields": "UID,NAME"
    })
    
    if isinstance(data_info, dict) and "error_code" in data_info:
        return None, f"Info Error: {data_info.get('error_msg')}"

    # Сохранение
    existing_groups = OkGroup.query.filter_by(project_id=project_id).all()
    existing_map = {g.group_id: g for g in existing_groups}
    
    items = data_info if isinstance(data_info, list) else [data_info]
    for info in items:
        gid = info.get('uid')
        if not gid: continue
        name = info.get('name', f"Группа {gid}")
        if gid not in existing_map:
            db.session.add(OkGroup(project_id=project_id, name=name, group_id=gid))
        else:
            existing_map[gid].name = name
    
    db.session.commit()
    return f"Синхронизировано {len(items)} групп.", None

# --------------------------------------------------------------------------
#  MAX MESSENGER
# --------------------------------------------------------------------------

def max_send_service(project_tokens, chat_id, text):
    token = project_tokens.max_token
    if not token: return None, "Нет токена MAX."
    try:
        url = "https://api.max-messenger.com/api/v1/send" 
        payload = {"chat_id": chat_id, "message": text}
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if not resp.ok: return None, f"MAX Error: {resp.text}"
        return "ok", None
    except Exception as e:
        return None, str(e)    


# --------------------------------------------------------------------------
#  INSTAGRAM
# --------------------------------------------------------------------------

def ig_get_public_url(filename):
    base_url = current_app.config.get('APP_URL') 
    if not base_url:
        try:
            base_url = url_for('main.index', _external=True)
        except Exception:
            return f"/static/uploads/{filename}" 
    return f"{base_url.rstrip('/')}/static/uploads/{filename}"

def ig_send_service(project_tokens, image_path, caption):
    IG_USER_ID = project_tokens.ig_user_id
    IG_TOKEN = project_tokens.ig_page_token 
    if not IG_USER_ID or not IG_TOKEN: return "Instagram не настроен."

    filename = os.path.basename(image_path)
    public_url = ig_get_public_url(filename)

    upload_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media",
        params={"image_url": public_url, "caption": caption[:2200], "access_token": IG_TOKEN},
        timeout=30
    )
    try:
        upload = upload_resp.json()
        if "error" in upload: return f"IG upload: {upload['error'].get('message')}"
        if "id" not in upload: return "IG upload: No ID."
        
        publish_resp = requests.post(
            f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media_publish",
            params={"creation_id": upload["id"], "access_token": IG_TOKEN},
            timeout=30
        )
        publish = publish_resp.json()
        if "error" in publish: return f"IG publish: {publish['error'].get('message')}"
    except Exception as e:
        return f"IG Error: {e}"
    return None   

# --------------------------------------------------------------------------
#  ЛОГИКА УДАЛЕНИЯ (CASCADING DELETE)
# --------------------------------------------------------------------------

def clear_tg_data(project_id):
    """
    Удаляет токен TG, все каналы и ВСЕ посты, связанные с TG каналами этого проекта.
    """
    try:
        # 1. Находим каналы проекта
        channels = TgChannel.query.filter_by(project_id=project_id).all()
        channel_ids = [c.id for c in channels]

        if channel_ids:
            # 2. Удаляем посты, привязанные к этим каналам
            # (Используем synchronize_session=False для массового удаления)
            Post.query.filter(Post.tg_channel_id.in_(channel_ids)).delete(synchronize_session=False)
            
            # 3. Удаляем RSS, привязанные к этим каналам
            RssSource.query.filter(RssSource.tg_channel_id.in_(channel_ids)).delete(synchronize_session=False)

            # 4. Удаляем сами каналы
            TgChannel.query.filter_by(project_id=project_id).delete(synchronize_session=False)

        # 5. Очищаем токен
        tokens = SocialTokens.query.filter_by(project_id=project_id).first()
        if tokens:
            tokens.tg_token = None
        
        db.session.commit()
        return True, "Данные Telegram очищены."
    except Exception as e:
        db.session.rollback()
        return False, str(e)

def clear_vk_data(project_id):
    """Удаляет токен VK, группы и посты."""
    try:
        groups = VkGroup.query.filter_by(project_id=project_id).all()
        group_ids = [g.id for g in groups]

        if group_ids:
            Post.query.filter(Post.vk_group_id.in_(group_ids)).delete(synchronize_session=False)
            RssSource.query.filter(RssSource.vk_group_id.in_(group_ids)).delete(synchronize_session=False)
            VkGroup.query.filter_by(project_id=project_id).delete(synchronize_session=False)

        tokens = SocialTokens.query.filter_by(project_id=project_id).first()
        if tokens:
            tokens.vk_token = None
            tokens.vk_refresh_token = None
            tokens._vk_token_encrypted = None # Если используется шифрование
            
        db.session.commit()
        return True, "Данные VK очищены."
    except Exception as e:
        db.session.rollback()
        return False, str(e)

def clear_ok_data(project_id):
    """Удаляет токен OK, группы и посты."""
    try:
        groups = OkGroup.query.filter_by(project_id=project_id).all()
        group_ids = [g.id for g in groups]

        if group_ids:
            Post.query.filter(Post.ok_group_id.in_(group_ids)).delete(synchronize_session=False)
            OkGroup.query.filter_by(project_id=project_id).delete(synchronize_session=False)

        tokens = SocialTokens.query.filter_by(project_id=project_id).first()
        if tokens:
            tokens.ok_token = None
            tokens.ok_refresh_token = None # Важно очистить рефреш
            
        db.session.commit()
        return True, "Данные OK очищены."
    except Exception as e:
        db.session.rollback()
        return False, str(e)

def clear_max_data(project_id):
    """Удаляет токен MAX, чаты и посты."""
    try:
        chats = MaxChat.query.filter_by(project_id=project_id).all()
        chat_ids = [c.id for c in chats]

        if chat_ids:
            Post.query.filter(Post.max_chat_id.in_(chat_ids)).delete(synchronize_session=False)
            MaxChat.query.filter_by(project_id=project_id).delete(synchronize_session=False)

        tokens = SocialTokens.query.filter_by(project_id=project_id).first()
        if tokens:
            tokens.max_token = None
            
        db.session.commit()
        return True, "Данные MAX очищены."
    except Exception as e:
        db.session.rollback()
        return False, str(e)

def delete_project_fully(project_id):
    """
    Полное удаление проекта со всеми зависимостями.
    Порядок: Посты -> RSS -> Каналы/Группы -> Токены -> Сброс active_project -> Проект.
    """
    try:
        # 1. Удаляем ВСЕ посты проекта
        Post.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        
        # 2. Удаляем ВСЕ RSS проекта
        RssSource.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        
        # 3. Удаляем все группы и каналы
        TgChannel.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        VkGroup.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        OkGroup.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        MaxChat.query.filter_by(project_id=project_id).delete(synchronize_session=False)
        
        # 4. Удаляем токены
        SocialTokens.query.filter_by(project_id=project_id).delete(synchronize_session=False)

        # 5. ВАЖНО: Отвязываем проект от пользователей (сбрасываем current_project_id)
        # Иначе будет ошибка ForeignKeyViolation, так как таблица users ссылается на этот проект
        User.query.filter_by(current_project_id=project_id).update({'current_project_id': None})
        
        # 6. Удаляем сам проект
        Project.query.filter_by(id=project_id).delete(synchronize_session=False)
        
        db.session.commit()
        return True, "Проект полностью удален."
    except Exception as e:
        db.session.rollback()
        return False, str(e)

# --------------------------------------------------------------------------
#  ГЛАВНАЯ ФОНОВАЯ ЗАДАЧА
# --------------------------------------------------------------------------

def publish_post_task(post_id):
    """
    Фоновая задача публикации поста во все соцсети.
    """
    from app import create_app, db
    import os
    import json
    from datetime import datetime
    
    # Импортируем модели и сервисы ВНУТРИ функции, чтобы избежать циклических ссылок
    from app.models import Post, Project, TgChannel, VkGroup, OkGroup, MaxChat, AppSettings
    # Убедитесь, что эти функции существуют в app.services или импортируйте их откуда нужно
    # Если они в этом же файле, импорт не нужен, но если они внешние - раскомментируйте:
    # from app.social_services import tg_send_service, vk_send_service, ig_send_service, ok_send_service, max_send_service
    
    app = create_app()
    
    with app.app_context():
        logger = app.logger
        logger.info(f"[Task: {post_id}] Начинаю публикацию...")
        
        # 1. Получаем глобальные настройки
        try:
            global_settings = AppSettings.get_settings()
        except Exception:
            # Если вдруг таблица не создана, создаем заглушку
            global_settings = type('obj', (object,), {'enable_email_posts': True})

        post = Post.query.get(post_id)
        if not post:
            logger.error(f"[Task: {post_id}] Пост не найден.")
            return

        project = post.project
        if not project:
            post.status = 'failed'
            post.error_message = 'Системная ошибка: нет проекта.'
            db.session.commit()
            return
            
        tokens = project.tokens
        if not tokens:
            post.status = 'failed'
            post.error_message = 'Не настроены соцсети в проекте (нет токенов).'
            db.session.commit()
            return      

        # Проверяем, выбрана ли хоть одна соцсеть
        destinations = [
            post.publish_to_tg, 
            post.publish_to_vk, 
            post.publish_to_ig, 
            post.publish_to_ok, 
            post.publish_to_max
        ]
        
        if not any(destinations):
            logger.warning(f"[Task: {post_id}] Не выбрана ни одна соцсеть.")
            post.status = 'failed'
            post.error_message = 'Ошибка: Вы не выбрали ни одну социальную сеть для публикации.'
            db.session.commit()
            return        
        
        # Ставим статус "В процессе"
        post.status = 'publishing'
        post.error_message = None 
        db.session.commit()

        # Подготовка файлов
        upload_folder = app.config['UPLOAD_FOLDER']
        media_files = post.media_files if post.media_files else []
        full_paths = [os.path.join(upload_folder, f) for f in media_files]
        
        images = [p for p in full_paths if p.lower().endswith(('.jpg', '.png', '.jpeg'))]
        
        platform_info = post.platform_info or {}
        errors = []
        buttons_json = json.dumps(platform_info.get('buttons', []))

        # ==========================================
        # 1. Telegram
        # ==========================================
        if post.publish_to_tg and post.tg_channel_id:
            if tokens.tg_token:
                channel = TgChannel.query.get(post.tg_channel_id)
                if channel:
                    try:
                        # ВАЖНО: Убедитесь, что tg_send_service доступна
                        msg_id, err = tg_send_service(tokens.tg_token, channel.chat_id, 
                                                      post.text, full_paths, buttons_json)                        
                        if err: 
                            errors.append(f"TG: {err}")
                        else: 
                            platform_info['tg_msg_id'] = msg_id
                    except Exception as e:
                        errors.append(f"TG Exception: {e}")
                else: 
                    errors.append("TG: Канал не найден.")
            else: 
                errors.append("TG: Токен не найден.")

        # ==========================================
        # 2. VK
        # ==========================================
        # if post.publish_to_vk and post.vk_group_id:
            # if platform_info.get('vk_post_id'):
                # logger.info(f"[Task: {post_id}] VK уже отправлен (ID: {platform_info['vk_post_id']}). Пропуск.")
            # else:
                # logger.info(f"[Task: {post_id}] Отправка в VK...")
                # vk_group = VkGroup.query.get(post.vk_group_id)
                
                # if vk_group and tokens:
                    # layout = post.vk_layout if hasattr(post, 'vk_layout') else 'grid'
                    
                    # vk_post_id, err = vk_send_service(tokens, vk_group.group_id, post.text_vk, full_paths, layout)
                    
                    # if err: 
                        # errors.append(f"VK: {err}")
                    # else: 
                        # platform_info['vk_post_id'] = vk_post_id
                        # logger.info(f"VK success: {vk_post_id}")
                # else:
                    # errors.append("VK: Группа не найдена или нет токенов")

        # ==========================================
        # 3. Instagram
        # ==========================================
        if post.publish_to_ig:
            if not images:
                errors.append("IG: Нужно фото.")
            else:
                try:
                    # ВАЖНО: Убедитесь, что ig_send_service доступна
                    err = ig_send_service(tokens, images[0], post.text_vk)
                    if err: 
                        errors.append(f"IG: {err}")
                except Exception as e: 
                    errors.append(f"IG Exception: {str(e)}")
                    
        # ==========================================
        # 4. OK (Odnoklassniki)
        # ==========================================
        if post.publish_to_ok and post.ok_group_id:
            ok_group = OkGroup.query.get(post.ok_group_id)
            if ok_group and tokens:
                try:
                    # ВАЖНО: Убедитесь, что ok_send_service доступна
                    post_id_ok, err = ok_send_service(tokens, ok_group.group_id, post.text_vk, full_paths)
                    if err: 
                        errors.append(f"OK: {err}")
                    else: 
                        platform_info['ok_post_id'] = post_id_ok
                except Exception as e:
                    errors.append(f"OK Exception: {e}")
            else:
                errors.append("OK: Группа/Токены не найдены")

        # ==========================================
        # 5. MAX (Messenger)
        # ==========================================
        if post.publish_to_max and post.max_chat_id:
            max_chat = MaxChat.query.get(post.max_chat_id)
            if max_chat and tokens:
                try:
                    # ВАЖНО: Убедитесь, что max_send_service доступна
                    _, err = max_send_service(tokens, max_chat.chat_id, post.text)
                    if err: 
                        errors.append(f"MAX: {err}")
                except Exception as e:
                    errors.append(f"MAX Exception: {e}")
            else:
                errors.append("MAX: Чат не найден")

        # ==========================================
        # Финализация статуса
        # ==========================================
        if errors:
            post.status = 'failed'
            # Если хотя бы куда-то ушло успешно (есть ID в platform_info), ставим 'partial'
            has_success = any(key.endswith('_id') for key in platform_info.keys())
            if has_success: 
                post.status = 'partial'
            
            post.error_message = " | ".join(errors)
        else:
            post.status = 'published'
            post.published_at = datetime.utcnow()
            
        post.platform_info = platform_info
        db.session.commit()
        
        logger.info(f"[Task: {post_id}] Завершено. Статус: {post.status}")

        # ==========================================
        # Отправка уведомлений на почту
        # ==========================================
        try:
            from app.email import send_email # Импорт здесь
            
            # 1. Проверяем глобальную настройку
            if global_settings.enable_email_posts:
                user = post.user
                
                # Письмо об УСПЕХЕ
                if post.status == 'published':
                    # Проверяем личную настройку (по умолчанию False, чтобы не спамить)
                    if hasattr(user, 'get_notification_setting') and \
                       user.get_notification_setting('email_post_success', False):
                        
                        send_email(
                            user.email, 
                            '✅ Пост опубликован', 
                            'email/post_success.html', 
                            post=post
                        )

                # Письмо об ОШИБКЕ или ЧАСТИЧНОМ успехе
                elif post.status in ['failed', 'partial']:
                    # Проверяем личную настройку (по умолчанию True)
                    if hasattr(user, 'get_notification_setting') and \
                       user.get_notification_setting('email_post_failed', True):
                        
                        send_email(
                            user.email, 
                            f'⚠️ Ошибка публикации ({post.status})', 
                            'email/post_failed.html', 
                            post=post
                        )
        except Exception as e_mail:
            logger.error(f"Error sending email notification: {e_mail}")

def check_expired_tariffs():
    """
    Фоновая задача: продление тарифов и уведомления.
    Запускается планировщиком.
    """
    
    # --- 1. МЕХАНИЗМ БЛОКИРОВКИ (ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА) ---
    # Это предотвращает запуск задачи во всех воркерах одновременно,
    # что спасает базу данных от перегрузки.
    lock_path = '/tmp/postbot_billing.lock'
    lock_file = open(lock_path, 'a')
    
    try:
        # Пытаемся захватить файл. Если занят — выходим.
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        # Задача уже выполняется другим процессом. Просто выходим.
        lock_file.close()
        return

    # --- 2. ОСНОВНАЯ ЛОГИКА ---
    try:
        # Импортируем create_app внутри, чтобы избежать циклических импортов
        from app import create_app
        app = create_app()
        
        with app.app_context():
            logger = app.logger
            # logger.info("Billing: Starting tariff check...")
            now = datetime.utcnow()
            
            # Получаем настройки
            try:
                global_settings = AppSettings.get_settings()
            except Exception as e:
                logger.error(f"Billing Error: Could not get settings: {e}")
                return

            # =========================================================
            # ЧАСТЬ 1: АВТОПРОДЛЕНИЕ
            # =========================================================
            expired_users = User.query.filter(
                User.tariff_expires_at < now,
                User.tariff_id.isnot(None) 
            ).all()
            
            users_renewed_count = 0
            
            for user in expired_users:
                current_tariff = user.tariff_rel
                
                # Пропускаем, если тарифа нет или он бесплатный (обычно их не надо продлевать за деньги)
                if not current_tariff or current_tariff.price == 0:
                    continue 

                if user.balance >= current_tariff.price:
                    try:
                        user.balance -= current_tariff.price
                        user.tariff_expires_at = now + timedelta(days=current_tariff.days)
                        
                        tx = Transaction(
                            user_id=user.id,
                            amount=-current_tariff.price,
                            type='auto_renewal',
                            # status='success', # Убедитесь, что поле status есть в модели Transaction, иначе уберите строку
                            description=f'Автопродление тарифа "{current_tariff.name}"'
                        )
                        db.session.add(tx)
                        users_renewed_count += 1
                        logger.info(f"Billing: User {user.id} renewed.")
                        
                    except Exception as e:
                        logger.error(f"Error renewing user {user.id}: {e}")
                        db.session.rollback()
                        continue
                else:
                    pass # Денег нет, остается с истекшей датой (функционал блокируется в других местах)

            db.session.commit()
            
            if users_renewed_count > 0:
                logger.info(f"Billing: Renewed {users_renewed_count} subscriptions.")

            # =========================================================
            # ЧАСТЬ 2: УВЕДОМЛЕНИЯ
            # =========================================================
            # Проверяем глобальную настройку уведомлений о тарифах
            if getattr(global_settings, 'enable_email_tariff', True):
                
                target_time_start = now + timedelta(days=3)
                target_time_end = target_time_start + timedelta(hours=1) 
                
                users_to_warn = User.query.filter(
                    User.tariff_expires_at >= target_time_start,
                    User.tariff_expires_at < target_time_end,
                    User.tariff_id.isnot(None)
                ).all()
                
                for user in users_to_warn:
                    if not user.tariff_rel or user.tariff_rel.price <= 0:
                        continue

                    # Проверяем личные настройки пользователя (если есть такой метод)
                    should_notify = True
                    if hasattr(user, 'get_notification_setting'):
                        should_notify = user.get_notification_setting('email_tariff_warning', True)
                        
                    if should_notify:
                        try:
                            # Убедитесь, что send_email импортирован
                            send_email(
                                to=user.email,
                                subject='⏳ Ваш тариф скоро истекает',
                                template='email/tariff_warning.html',
                                user=user,
                                days_left=3,
                                tariff_name=user.tariff_rel.name,
                                price=user.tariff_rel.price
                            )
                            logger.info(f"Tariff warning sent to {user.email}")
                        except Exception as e:
                            logger.error(f"Failed to send warning to {user.email}: {e}")

            # =========================================================
            # ВАЖНО: ЗАКРЫТИЕ СОЕДИНЕНИЙ
            # =========================================================
            # Это решает проблему "FATAL: remaining connection slots..."
            # Мы принудительно закрываем сессию и пул соединений этого временного app.
            db.session.remove()
            db.engine.dispose()

    except Exception as e_global:
        # Ловим критические ошибки самого процесса
        print(f"Critical Billing Error: {e_global}")

    finally:
        # --- 3. СНЯТИЕ БЛОКИРОВКИ ---
        # Всегда освобождаем файл, даже если произошла ошибка
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()