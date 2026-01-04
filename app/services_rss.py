# app/services_rss.py
import feedparser
import requests
import os
import uuid
import fcntl # <-- Для блокировки процессов
import logging
from bs4 import BeautifulSoup
from flask import current_app
from app import db
from app.models import RssSource, Post
from app.services import publish_post_task
from datetime import datetime

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
    
    # --- МЕХАНИЗМ БЛОКИРОВКИ (С ЗАЩИТОЙ ОТ GUNICORN) ---
    # Создаем/открываем файл-лок
    lock_path = '/tmp/postbot_rss.lock'
    # 'a' - режим добавления. Он не затирает файл при открытии.
    lock_file = open(lock_path, 'a')
    
    try:
        # Пытаемся получить эксклюзивный доступ к файлу.
        # fcntl.LOCK_EX - эксклюзивный замок
        # fcntl.LOCK_NB - не ждать (Non-Blocking), если занято - сразу выдать ошибку
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        # Если файл занят другим воркером, просто выходим
        # logger.info("RSS: Задача уже выполняется другим процессом. Пропуск.")
        lock_file.close()
        return

    # --- ЕСЛИ МЫ ТУТ, ЗНАЧИТ МЫ ПЕРВЫЕ ЗАХВАТИЛИ ФАЙЛ ---
    try:
        from run import app
        with app.app_context():
            # logger.info("RSS: Start parsing...")
            sources = RssSource.query.filter_by(is_active=True).all()
            
            for source in sources:
                try:
                    feed = feedparser.parse(source.url)
                    if not feed.entries:
                        continue
                    
                    # Ищем новые посты
                    new_entries = []
                    # last_guid может быть None, если это первый запуск
                    last_guid = source.last_guid
                    
                    # Пробегаем по ленте сверху вниз
                    for entry in feed.entries:
                        guid = entry.get('id', entry.get('link'))
                        
                        # Если встретили пост, который уже был - останавливаемся
                        if guid == last_guid:
                            break 
                        
                        new_entries.append(entry)
                    
                    # Если постов много, а last_guid пустой (первый прогон),
                    # берем только 1 самый свежий, чтобы не заспамить канал 20-ю постами.
                    if not last_guid and new_entries:
                        # logger.info(f"RSS: Первый запуск для {source.name}, берем только последний пост.")
                        new_entries = [new_entries[0]]
                    
                    # Если постов слишком много (например, сайт лежал и вывалил 50 штук),
                    # ограничим пачку до 5, чтобы не получить бан от Telegram
                    if len(new_entries) > 5:
                        new_entries = new_entries[:5]

                    # Постим в хронологическом порядке (от старых к новым)
                    for entry in reversed(new_entries):
                        # ВАЖНО: Проверяем еще раз GUID перед обработкой, 
                        # на случай если другой поток успел записать (двойная страховка)
                        guid = entry.get('id', entry.get('link'))
                        
                        # Пробуем обработать
                        process_entry(source, entry)
                        
                        # Сразу обновляем last_guid в базе после каждого поста!
                        # Это защитит, если скрипт упадет на середине.
                        source.last_guid = guid
                        db.session.commit()
                        
                except Exception as e:
                    logger.error(f"RSS: Error parsing {source.url}: {e}")
                    db.session.rollback() # Откат базы при ошибке

    finally:
        # В конце ОБЯЗАТЕЛЬНО снимаем замок
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

def process_entry(source, entry):
    """Обработка одной записи RSS и создание поста"""
    title = entry.get('title', 'Без заголовка')
    link = entry.get('link', '')
    description = entry.get('summary', entry.get('description', ''))
    
    # 1. Вытаскиваем картинку
    image_url = None
    
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.type.startswith('image/'):
                image_url = enc.href
                break
                
    if not image_url and 'media_content' in entry:
        media = entry.media_content[0]
        if 'url' in media: image_url = media['url']
            
    if not image_url:
        soup = BeautifulSoup(description, 'html.parser')
        img_tag = soup.find('img')
        if img_tag and img_tag.get('src'): image_url = img_tag['src']
            
    # 2. Текст
    text_html = f"<b>{title}</b>\n\n<a href='{link}'>Читать далее</a>"
    
    # 3. Скачиваем файл
    media_files = []
    if image_url:
        saved_filename = download_image(image_url)
        if saved_filename:
            media_files.append(saved_filename)
            
    # 4. Создаем пост
    new_post = Post(
        user_id=source.user_id,
        text=text_html,
        text_vk=f"{title}\n\n{link}",
        media_files=media_files,
        status='scheduled', # Ставим scheduled
        scheduled_at=datetime.utcnow(),
        
        publish_to_tg=source.publish_to_tg,
        tg_channel_id=source.tg_channel_id,
        
        publish_to_vk=source.publish_to_vk,
        vk_group_id=source.vk_group_id,
        vk_layout='grid',
        
        # publish_to_max=source.publish_to_max
    )
    
    db.session.add(new_post)
    db.session.commit()
    
    # 5. Триггерим
    logger.info(f"RSS: Post created from {source.name} (ID: {new_post.id})")
    publish_post_task(new_post.id)