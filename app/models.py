# app/models.py
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.utils import encrypt_data, decrypt_data
from datetime import datetime

@login_manager.user_loader
def load_user(user_id):
    """Callback-функция для Flask-Login для загрузки пользователя по ID."""
    return User.query.get(int(user_id))

class User(UserMixin, db.Model):
    """Обновленная модель пользователя."""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    # 'username' заменен на 'email'
    email = db.Column(db.String(120), unique=True, nullable=False, index=True) 
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # is_active=False - пользователь не может войти, пока не кликнет по ссылке
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    
    current_project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)    
    projects = db.relationship('Project', foreign_keys=[Project.user_id], backref='owner', lazy=True)    
    
    # Храним баланс в КОПЕЙКАХ (целое число), чтобы избежать ошибок float
    balance = db.Column(db.Integer, nullable=False, default=0)
    
    timezone = db.Column(db.String(50), default='UTC') # Например: 'Europe/Moscow'
    tariff = db.Column(db.String(20), default='mini')  # 'mini', 'middle', 'maxi'
    is_setup_complete = db.Column(db.Boolean, default=False) # Флаг: прошел ли первичную настройку    

    # Связи (ленивая загрузка)
    tokens = db.relationship('SocialTokens', backref='user', uselist=False, 
                             lazy=True, cascade="all, delete-orphan")
    tg_channels = db.relationship('TgChannel', backref='user', lazy=True, 
                                  cascade="all, delete-orphan")
    vk_groups = db.relationship('VkGroup', backref='user', lazy=True, 
                                cascade="all, delete-orphan")
    posts = db.relationship('Post', backref='user', lazy=True, 
                            cascade="all, delete-orphan")

    def set_password(self, password):
        """Устанавливает хэш пароля."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Проверяет пароль."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.email}>'

class SocialTokens(db.Model):
    __tablename__ = 'social_tokens'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    
    _tg_token_encrypted = db.Column(db.String(1024))
    _vk_token_encrypted = db.Column(db.String(1024))
    _ig_page_token_encrypted = db.Column(db.String(1024))
    ig_user_id = db.Column(db.String(256)) 

    _vk_refresh_token_encrypted = db.Column(db.String(1024))
    vk_device_id = db.Column(db.String(256)) # ID устройства
    vk_token_expires_at = db.Column(db.DateTime, nullable=True) # Когда токен "умрет"

    # Мы используем @property для прозрачного шифрования/дешифрования
    
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

class TgChannel(db.Model):
    """Каналы Telegram, привязанные к пользователю."""
    __tablename__ = 'tg_channels'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    chat_id = db.Column(db.String(255), nullable=False)

class VkGroup(db.Model):
    """Группы VK, привязанные к пользователю."""
    __tablename__ = 'vk_groups'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    group_id = db.Column(db.BigInteger, nullable=False)

class Signature(db.Model):
    """Шаблоны подписей."""
    __tablename__ = 'signatures'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False) # Название (например, "Для рекламы")
    text = db.Column(db.Text, nullable=False)        # Текст подписи
    
    # Связь с пользователем (backref добавит user.signatures)
    user = db.relationship('User', backref=db.backref('signatures', lazy=True, cascade="all, delete-orphan"))

class Post(db.Model):
    """История постов."""
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)    
    
    text = db.Column(db.Text, nullable=False)
    text_vk = db.Column(db.Text)
    
    # 2. Меняем JSON на JSONB для лучшей производительности в Postgres
    media_files = db.Column(JSONB) # ['img1.jpg', 'vid1.mp4']
    
    # 'scheduled' | 'publishing' | 'published' | 'failed'
    status = db.Column(db.String(50), default='scheduled', nullable=False)
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    scheduled_at = db.Column(db.DateTime, nullable=True) # Время по UTC
    published_at = db.Column(db.DateTime, nullable=True)

    # 3. Меняем JSON на JSONB
    platform_info = db.Column(JSONB) # {'tg_msg_id': 123, 'vk_post_id': 456}

    # Цели публикации (чтобы знать, куда публиковать)
    publish_to_tg = db.Column(db.Boolean, default=False)
    publish_to_vk = db.Column(db.Boolean, default=False)
    publish_to_ig = db.Column(db.Boolean, default=False)
    
    # ID каналов/групп, *выбранных для этого поста*
    tg_channel_id = db.Column(db.Integer, db.ForeignKey('tg_channels.id'))
    vk_group_id = db.Column(db.Integer, db.ForeignKey('vk_groups.id'))
    
    vk_layout = db.Column(db.String(50), default='grid')
    
class RssSource(db.Model):
    __tablename__ = 'rss_sources'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True) 
    
    name = db.Column(db.String(100))        # Название (например "Новости Lenta.ru")
    url = db.Column(db.String(512), nullable=False) # Ссылка на RSS
    
    # Настройки публикации (куда отправлять новые посты отсюда)
    publish_to_tg = db.Column(db.Boolean, default=False)
    tg_channel_id = db.Column(db.Integer, db.ForeignKey('tg_channels.id'), nullable=True)
    
    publish_to_vk = db.Column(db.Boolean, default=False)
    vk_group_id = db.Column(db.Integer, db.ForeignKey('vk_groups.id'), nullable=True)
    
    publish_to_max = db.Column(db.Boolean, default=False)
    
    # Служебные поля для отслеживания новинок
    last_guid = db.Column(db.String(512))   # ID последнего обработанного поста
    is_active = db.Column(db.Boolean, default=True)
    
    user = db.relationship('User', backref=db.backref('rss_sources', lazy=True)) 

class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), default="Мой проект")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    posts = db.relationship('Post', backref='project', lazy=True, cascade="all, delete-orphan")
    tg_channels = db.relationship('TgChannel', backref='project', lazy=True, cascade="all, delete-orphan")
    vk_groups = db.relationship('VkGroup', backref='project', lazy=True, cascade="all, delete-orphan")
    rss_sources = db.relationship('RssSource', backref='project', lazy=True, cascade="all, delete-orphan")    