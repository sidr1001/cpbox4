# app/models.py
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
# from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.utils import encrypt_data, decrypt_data
# from sqlalchemy.dialects.postgresql import JSON

@login_manager.user_loader
def load_user(user_id):
    """Callback-функция для Flask-Login для загрузки пользователя по ID."""
    return User.query.get(int(user_id))

# --- ПЕРЕНЕСЛИ КЛАСС PROJECT СЮДА (ВВЕРХ) ---
class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    # Используем строковую ссылку 'users.id', поэтому порядок определения таблиц для FK не важен
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), default="Мой проект")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    posts = db.relationship('Post', backref='project', lazy=True, cascade="all, delete-orphan")
    tg_channels = db.relationship('TgChannel', backref='project', lazy=True, cascade="all, delete-orphan")
    vk_groups = db.relationship('VkGroup', backref='project', lazy=True, cascade="all, delete-orphan")
    rss_sources = db.relationship('RssSource', backref='project', lazy=True, cascade="all, delete-orphan")
    tokens = db.relationship('SocialTokens', backref='project', uselist=False, lazy=True, cascade="all, delete-orphan")    
    ok_groups = db.relationship('OkGroup', backref='project', lazy=True, cascade="all, delete-orphan")
    max_chats = db.relationship('MaxChat', backref='project', lazy=True, cascade="all, delete-orphan")

class Tariff(db.Model):
    __tablename__ = 'tariffs'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # Название: "Базовый", "PRO"
    slug = db.Column(db.String(50), unique=True)     # Код: 'basic', 'pro' (для логики в коде)
    price = db.Column(db.Integer, default=0)         # Цена в рублях (или копейках)
    days = db.Column(db.Integer, default=30)         # Длительность (30 дней)
    
    # --- ЛИМИТЫ (Жесткие колонки для SQL запросов) ---
    max_projects = db.Column(db.Integer, default=1)
    max_posts_per_month = db.Column(db.Integer, default=50)
    
    # --- ГИБКИЕ НАСТРОЙКИ (JSON) ---
    # Пример: {"allow_vk": true, "allow_ok": false, "max_files_size_mb": 10}
    options = db.Column(db.JSON, default={}) 
    
    is_active = db.Column(db.Boolean, default=True)  # Чтобы скрыть архивные тарифы

    def __repr__(self):
        return self.name

# --- КЛАСС USER ИДЕТ ПОСЛЕ PROJECT ---
class User(UserMixin, db.Model):
    """Обновленная модель пользователя."""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True) 
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    notification_settings = db.Column(db.JSON, default={})
    
    current_project_id = db.Column(
        db.Integer, 
        db.ForeignKey('projects.id', use_alter=True, name='fk_user_current_project'), 
        nullable=True
    )   
    
    # Теперь Project определен, и мы можем ссылаться на Project.user_id
    projects = db.relationship('Project', foreign_keys=[Project.user_id], backref='owner', lazy=True)    
    
    balance = db.Column(db.Integer, nullable=False, default=0)
    
    timezone = db.Column(db.String(50), default='UTC')
    # tariff = db.Column(db.String(20), default='mini')
    is_setup_complete = db.Column(db.Boolean, default=False)
    
    tariff_id = db.Column(db.Integer, db.ForeignKey('tariffs.id'), nullable=True)
    
    # Добавляем поле даты окончания тарифа
    tariff_expires_at = db.Column(db.DateTime, nullable=True)
    
    # Дата последнего изменения тарифа
    last_tariff_change = db.Column(db.DateTime, nullable=True)
    
    # Связь с транзакциями
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade="all, delete-orphan")    

    # Связь (отношение), чтобы писать user.tariff.name
    tariff_rel = db.relationship('Tariff', backref='users')


    # Связи
    tg_channels = db.relationship('TgChannel', backref='user', lazy=True, 
                                  cascade="all, delete-orphan")
    vk_groups = db.relationship('VkGroup', backref='user', lazy=True, 
                                cascade="all, delete-orphan")
    posts = db.relationship('Post', backref='user', lazy=True, 
                            cascade="all, delete-orphan")
                            
    @property
    def current_project_tokens(self):
        """Возвращает токены активного проекта пользователя."""
        if self.current_project_id and self.projects:
            for p in self.projects:
                if p.id == self.current_project_id:
                    return p.tokens
        return None    

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.email}>'
        
    @property
    def current_tariff(self):
        """Возвращает объект тарифа. Если тарифа нет — вернет None."""
        # Тут можно добавить логику: если срок истёк, возвращать тариф 'mini'
        return self.tariff_rel

    def get_limit(self, limit_name):
        """
        Универсальный метод получения лимита.
        Сначала ищет в колонках (жесткие лимиты), потом в JSON (гибкие).
        """
        if not self.current_tariff:
            # Дефолтные лимиты для юзера без тарифа (например, как на MINI)
            defaults = {'max_projects': 1, 'max_posts_per_month': 50}
            return defaults.get(limit_name, 0)
            
        # 1. Пробуем найти жесткую колонку (max_projects)
        if hasattr(self.current_tariff, limit_name):
            return getattr(self.current_tariff, limit_name)
            
        # 2. Иначе ищем в JSON options (allow_vk, max_file_size)
        return self.current_tariff.options.get(limit_name, False)    

    def is_tariff_active(self):
        """
        Проверяет, активен ли тариф.
        Если дата окончания прошла — тариф неактивен (блокировка).
        Если даты нет (None) — считаем, что это ненормально для платной системы, 
        но для обратной совместимости можно считать активным (или наоборот).
        В новой логике: при регистрации дата ставится всегда.
        """
        if not self.tariff_expires_at:
            # Если даты нет, допустим, это вечный админ или старый юзер.
            # Либо возвращаем False, если хотите жестко всех заставить платить.
            return True 
            
        return datetime.utcnow() < self.tariff_expires_at
        
    def get_notification_setting(self, key, default=True):
        """
        Безопасно получает настройку.
        key: название настройки (например, 'email_payment_success')
        default: что вернуть, если пользователь еще ничего не настраивал (обычно True - включено)
        """
        if not self.notification_settings:
            return default
        
        # .get() ищет ключ, а если не находит — возвращает default
        return self.notification_settings.get(key, default)        
        
    def get_days_left(self):
        """Возвращает количество полных дней до истечения тарифа."""
        if not self.tariff_expires_at:
            return None
        delta = self.tariff_expires_at - datetime.utcnow()
        return delta.days

    def can_create_project(self):
        """Проверка лимита проектов."""
        # 1. ЖЕСТКАЯ БЛОКИРОВКА ПО ВРЕМЕНИ
        if not self.is_tariff_active():
            return False, "Срок действия тарифа истек. Пополните счет для разблокировки."
            
        # 2. Получаем лимит
        limit = self.get_limit('max_projects')
        
        # 3. Считаем текущие
        current_count = Project.query.filter_by(user_id=self.id).count()
        
        if current_count >= limit:
            return False, f"Достигнут лимит проектов ({limit}). Обновите тариф."
        return True, "OK"

    def can_create_post(self):
        """Проверка лимита постов."""
        # 1. ЖЕСТКАЯ БЛОКИРОВКА ПО ВРЕМЕНИ
        if not self.is_tariff_active():
            return False, "Срок действия тарифа истек. Пополните счет для разблокировки."

        limit = self.get_limit('max_posts_per_month')
        
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
        posts_count = Post.query.filter_by(user_id=self.id)\
            .filter(Post.created_at >= month_start).count()
            
        if posts_count >= limit:
            return False, f"Лимит постов на этот месяц исчерпан ({limit})."
        return True, "OK"       

class SocialTokens(db.Model):
    __tablename__ = 'social_tokens'
    
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    
    _tg_token_encrypted = db.Column(db.String(1024))
    _vk_token_encrypted = db.Column(db.String(1024))
    _ig_page_token_encrypted = db.Column(db.String(1024))
    ig_user_id = db.Column(db.String(256)) 

    _vk_refresh_token_encrypted = db.Column(db.String(1024))
    vk_device_id = db.Column(db.String(256))
    vk_token_expires_at = db.Column(db.DateTime, nullable=True)
    
    # --- OK ---
    _ok_token_encrypted = db.Column(db.String(1024))
    _ok_refresh_token_encrypted = db.Column(db.String(1024))
    ok_app_pub_key = db.Column(db.String(256))     # Публичный ключ приложения
    ok_app_secret_key = db.Column(db.String(256))  # Секретный ключ приложения (для подписи)
    
    # --- MAX ---
    _max_token_encrypted = db.Column(db.String(1024))

    # (Геттеры и сеттеры)
    @property
    def tg_token(self):
        return decrypt_data(self._tg_token_encrypted)

    @tg_token.setter
    def tg_token(self, value):
        self._tg_token_encrypted = encrypt_data(value)

    @property
    def vk_token(self):
        return decrypt_data(self._vk_token_encrypted)

    @vk_token.setter
    def vk_token(self, value):
        self._vk_token_encrypted = encrypt_data(value)

    @property
    def ig_page_token(self):
        return decrypt_data(self._ig_page_token_encrypted)

    @ig_page_token.setter
    def ig_page_token(self, value):
        self._ig_page_token_encrypted = encrypt_data(value)
        
    @property
    def vk_refresh_token(self):
        return decrypt_data(self._vk_refresh_token_encrypted)

    @vk_refresh_token.setter
    def vk_refresh_token(self, value):
        self._vk_refresh_token_encrypted = encrypt_data(value)  

    @property
    def ok_token(self):
        return decrypt_data(self._ok_token_encrypted)

    @ok_token.setter
    def ok_token(self, value):
        self._ok_token_encrypted = encrypt_data(value)
        
    @property
    def ok_refresh_token(self):
        return decrypt_data(self._ok_refresh_token_encrypted)

    @ok_refresh_token.setter
    def ok_refresh_token(self, value):
        self._ok_refresh_token_encrypted = encrypt_data(value)          

    @property
    def max_token(self):
        return decrypt_data(self._max_token_encrypted)

    @max_token.setter
    def max_token(self, value):
        self._max_token_encrypted = encrypt_data(value)        

class TgChannel(db.Model):
    __tablename__ = 'tg_channels'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    chat_id = db.Column(db.String(255), nullable=False)

class VkGroup(db.Model):
    __tablename__ = 'vk_groups'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    group_id = db.Column(db.BigInteger, nullable=False)
    
class OkGroup(db.Model):
    __tablename__ = 'ok_groups'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    group_id = db.Column(db.String(255), nullable=False) # ID группы в ОК

class MaxChat(db.Model):
    __tablename__ = 'max_chats'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    chat_id = db.Column(db.String(255), nullable=False) # ID чата в MAX    

class Signature(db.Model):
    __tablename__ = 'signatures'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    text = db.Column(db.Text, nullable=False)
    
    user = db.relationship('User', backref=db.backref('signatures', lazy=True, cascade="all, delete-orphan"))

class Post(db.Model):
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)    
    
    text = db.Column(db.Text, nullable=False)
    text_vk = db.Column(db.Text)
    
    media_files = db.Column(db.JSON)
    
    status = db.Column(db.String(50), default='scheduled', nullable=False)
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    scheduled_at = db.Column(db.DateTime, nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)

    platform_info = db.Column(db.JSON)

    publish_to_tg = db.Column(db.Boolean, default=False)
    publish_to_vk = db.Column(db.Boolean, default=False)
    publish_to_ig = db.Column(db.Boolean, default=False)
    publish_to_ok = db.Column(db.Boolean, default=False)
    publish_to_max = db.Column(db.Boolean, default=False)  
    
    tg_channel_id = db.Column(db.Integer, db.ForeignKey('tg_channels.id'))
    vk_group_id = db.Column(db.Integer, db.ForeignKey('vk_groups.id'))
    ok_group_id = db.Column(db.Integer, db.ForeignKey('ok_groups.id'))
    max_chat_id = db.Column(db.Integer, db.ForeignKey('max_chats.id'))       
    
    vk_layout = db.Column(db.String(50), default='grid')   
    
class RssSource(db.Model):
    __tablename__ = 'rss_sources'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True) 
    
    name = db.Column(db.String(100))
    url = db.Column(db.String(512), nullable=False)
    
    publish_to_tg = db.Column(db.Boolean, default=False)
    tg_channel_id = db.Column(db.Integer, db.ForeignKey('tg_channels.id'), nullable=True)
    
    publish_to_vk = db.Column(db.Boolean, default=False)
    vk_group_id = db.Column(db.Integer, db.ForeignKey('vk_groups.id'), nullable=True)

    publish_to_ok = db.Column(db.Boolean, default=False)
    ok_group_id = db.Column(db.String(50), nullable=True)    
    
    publish_to_max = db.Column(db.Boolean, default=False)
    
    last_guid = db.Column(db.String(512))
    is_active = db.Column(db.Boolean, default=True)
    
    user = db.relationship('User', backref=db.backref('rss_sources', lazy=True))
    
# Транзакции
class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Сумма в копейках. 
    # Отрицательное значение = списание (оплата тарифа).
    # Положительное = пополнение.
    amount = db.Column(db.Integer, nullable=False) 
    
    # Тип операции: 'tariff_payment', 'deposit', 'refund', 'correction'
    type = db.Column(db.String(50), nullable=False)
    
    # Описание: "Оплата тарифа PRO на 30 дней"
    description = db.Column(db.String(255))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    status = db.Column(db.String(20), default='success') # 'pending', 'success', 'failed'
    provider = db.Column(db.String(20)) # 'cloudpayments', 'unitpay', 'manual'
    external_id = db.Column(db.String(100)) # ID транзакции в платежной системе    

    def __repr__(self):
        return f"<Transaction {self.id} {self.amount} {self.status}>"    
        
class PromoCode(db.Model):
    __tablename__ = 'promocodes'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    
    # Два типа скидки:
    discount_percent = db.Column(db.Integer, default=0) # Процент (0-100)
    discount_amount = db.Column(db.Integer, default=0)  # Фикс. сумма в копейках
    
    valid_until = db.Column(db.DateTime, nullable=True)
    usage_limit = db.Column(db.Integer, default=0)
    times_used = db.Column(db.Integer, default=0)
    
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        if self.discount_percent > 0:
            return f"<PromoCode {self.code} - {self.discount_percent}%>"
        return f"<PromoCode {self.code} - {self.discount_amount / 100} RUB>"

class AppSettings(db.Model):
    """Глобальные настройки приложения (храним в БД)."""
    __tablename__ = 'app_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    # Храним список активных провайдеров через запятую: "cloudpayments,yookassa"
    active_payment_providers = db.Column(db.String(255), default='cloudpayments') 

    @classmethod
    def get_settings(cls):
        """Получить настройки (или создать дефолтные, если нет)."""
        settings = cls.query.first()
        if not settings:
            settings = cls(active_payment_providers='cloudpayments,unitpay')
            db.session.add(settings)
            db.session.commit()
        return settings        