# app/services_rss.py
import feedparser
import re
import requests
import os
import uuid
import fcntl
import logging
from bs4 import BeautifulSoup, NavigableString
from flask import current_app
from app import db
from app.models import RssSource, Post, Project
from app.services import publish_post_task
from datetime import datetime
from app import create_app

# Настройка логгера
logger = logging.getLogger(__name__)

def download_image(img_url):
    """Скачивает картинку по URL и сохраняет в папку uploads"""
    if not img_url: return None
    try:
        # Генерируем имя
        ext = os.path.splitext(img_url)[1].split('?')[0]
        if not ext: ext = '.jpg'
        filename = f"{uuid.uuid4()}{ext}"
        
        # Скачиваем
        r = requests.get(img_url, timeout=10, stream=True)
        if r.status_code == 200:
            upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            with open(upload_path, 'wb') as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            return filename
    except Exception as e:
        logger.error(f"RSS: Error downloading image {img_url}: {e}")
    return None

def parse_rss_feeds():
    """Эта функция запускается по расписанию"""
    
    # --- 1. БЛОКИРОВКА (Оставляем как есть) ---
    lock_path = '/tmp/postbot_rss.lock'
    lock_file = open(lock_path, 'a')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        lock_file.close()
        return

    # --- 2. ЛОГИКА ---
    try:
        app = create_app() 
        with app.app_context():
            logger = app.logger
            # print("1. Ищу источники...")
            
            sources = RssSource.query.filter_by(is_active=True).all()
            
            for source in sources:
                try:
                    feed = feedparser.parse(source.url)
                    if not feed.entries:
                        continue
                    
                    # Берем 10 последних
                    entries_to_process = feed.entries[:10]
                    
                    # Идем от старых к новым
                    for entry in reversed(entries_to_process):
                        
                        # 1. GUID
                        guid = getattr(entry, 'id', getattr(entry, 'link', getattr(entry, 'title', None)))
                        if not guid: continue

                        # 2. Проверка дублей в БД
                        existing_post = Post.query.filter_by(rss_guid=guid).first()
                        if existing_post:
                            continue
                        
                        # 3. Проверка по last_guid
                        if source.last_guid and guid == source.last_guid:
                            continue

                        # 4. ВЫЗЫВАЕМ УМНУЮ ОБРАБОТКУ (Картинки + Текст)
                        print(f"      [NEW] Обрабатываю: {entry.title[:30]}")
                        
                        # Мы передаем работу функции process_entry
                        process_entry(source, entry, guid)
                        
                        # Обновляем last_guid
                        source.last_guid = guid
                        db.session.commit() # Сохраняем обновление GUID источника

                except Exception as e:
                    print(f"!!! ОШИБКА в источнике {source.url}: {e}")
                    logger.error(f"RSS Error: {e}")
                    db.session.rollback()

    except Exception as e_global:
        print(f"Critical RSS Error: {e_global}")

    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

def process_entry(source, entry, guid):
    """Обработка одной записи RSS и создание поста"""

    title = entry.get('title', 'Без заголовка')
    link = entry.get('link', '')

    description_raw = entry.get('summary') or entry.get('description') or ''

    description_html = ""
    description_plain = ""

    if description_raw:
        try:
            soup = BeautifulSoup(description_raw, 'html.parser')

            # удаляем мусор
            for tag in soup(["script", "style"]):
                tag.extract()

            # <br> -> \n (базовый смысл RSS)
            for br in soup.find_all("br"):
                br.replace_with("\n")

            # приводим теги к Telegram HTML
            for tag in soup.find_all('strong'):
                tag.name = 'b'
            for tag in soup.find_all('em'):
                tag.name = 'i'

            # -------- получаем чистые строки --------
            raw_lines = [
                line.strip()
                for line in soup.get_text().split("\n")
                if line.strip()
            ]

            def is_param_line(line: str) -> bool:
                """
                Строка-параметр:
                - есть двоеточие
                - двоеточие в первой половине строки
                """
                pos = line.find(":")
                return 0 < pos < len(line) * 0.6

            # -------- собираем текст с логикой --------
            result_lines = []
            prev_was_param = False

            for line in raw_lines:
                cur_is_param = is_param_line(line)

                if result_lines:
                    # пустая строка только если это НЕ два параметра подряд
                    if not (prev_was_param and cur_is_param):
                        result_lines.append("")

                result_lines.append(line)
                prev_was_param = cur_is_param

            description_plain = "\n".join(result_lines).strip()

            # -------- HTML для Telegram --------
            # оставляем только допустимые теги
            allowed_tags = ['b', 'i', 'a', 'u', 's', 'code', 'pre']
            for tag in soup.find_all(True):
                if tag.name not in allowed_tags:
                    tag.unwrap()

            description_html = description_plain

        except Exception as e:
            logger.error(f"RSS: Ошибка парсинга: {e}")
            description_plain = description_raw[:800]
            description_html = description_raw[:800]

    # -------- ИЗОБРАЖЕНИЕ --------
    image_url = None

    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.type.startswith('image/'):
                image_url = enc.href
                break

    if not image_url and 'media_content' in entry:
        media = entry.media_content[0]
        if 'url' in media:
            image_url = media['url']

    if not image_url and description_raw:
        try:
            raw_soup = BeautifulSoup(description_raw, 'html.parser')
            img = raw_soup.find('img')
            if img and img.get('src'):
                image_url = img['src']
        except:
            pass

    # -------- TELEGRAM --------
    text_tg = f"<b>{title}</b>\n{link}\n\n"
    if description_html:
        text_tg += description_html

    # защита от случайных множественных переносов
    text_tg = re.sub(r'\n{3,}', '\n\n', text_tg).strip()

    # -------- VK / OK --------
    text_vk_ok = f"{title}\n"
    if description_plain:
        text_vk_ok += f"{description_plain}\n"
    text_vk_ok += f"{link}"

    # -------- МЕДИА --------
    media_files = []
    if image_url:
        saved_filename = download_image(image_url)
        if saved_filename:
            media_files.append(saved_filename)
            
    # --- НАЧАЛО ВСТАВКИ: УМНАЯ ОБРЕЗКА ДЛЯ RSS ---
    # Определяем лимит в зависимости от того, нашлась ли картинка
    tg_limit = 1024 if media_files else 4096
    
    if len(text_tg) > tg_limit:
        # Обрезаем лишнее
        text_tg = text_tg[:tg_limit - 3] + "..."
    # --- КОНЕЦ ВСТАВКИ ---            

    # -------- СОЗДАНИЕ ПОСТА --------
    new_post = Post(
        user_id=source.user_id,
        project_id=source.project_id,

        text=text_tg,
        text_vk=text_vk_ok,
        media_files=media_files,

        status='scheduled',
        scheduled_at=datetime.utcnow(),

        publish_to_tg=source.publish_to_tg,
        tg_channel_id=source.tg_channel_id,

        publish_to_vk=source.publish_to_vk,
        vk_group_id=source.vk_group_id,
        vk_layout='grid',

        publish_to_ok=source.publish_to_ok,
        ok_group_id=source.ok_group_id,
       
        # publish_to_ig=getattr(source, 'publish_to_ig', False), # Instagram 
        
        publish_to_max=source.publish_to_max,
        max_chat_id=source.max_chat_id,
        # ----------------------------------------------------        
        
        rss_guid=guid,
    )

    db.session.add(new_post)
    db.session.commit()

    logger.info(f"RSS: Post created from {source.name} (ID: {new_post.id})")
    publish_post_task(new_post.id)

