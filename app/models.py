# app/models.py
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.utils import encrypt_data, decrypt_data

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
    
    current_project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)    
    
    # Теперь Project определен, и мы можем ссылаться на Project.user_id
    projects = db.relationship('Project', foreign_keys=[Project.user_id], backref='owner', lazy=True)    
    
    balance = db.Column(db.Integer, nullable=False, default=0)
    
    timezone = db.Column(db.String(50), default='UTC')
    tariff = db.Column(db.String(20), default='mini')
    is_setup_complete = db.Column(db.Boolean, default=False)

    # Связи
    tg_channels = db.relationship('TgChannel', backref='user', lazy=True, 
                                  cascade="all, delete-orphan")
    vk_groups = db.relationship('VkGroup', backref='user', lazy=True, 
                                cascade="all, delete-orphan")
    posts = db.relationship('Post', backref='user', lazy=True, 
                            cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.email}>'

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
    
    media_files = db.Column(JSONB)
    
    status = db.Column(db.String(50), default='scheduled', nullable=False)
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    scheduled_at = db.Column(db.DateTime, nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)

    platform_info = db.Column(JSONB)

    publish_to_tg = db.Column(db.Boolean, default=False)
    publish_to_vk = db.Column(db.Boolean, default=False)
    publish_to_ig = db.Column(db.Boolean, default=False)
    
    tg_channel_id = db.Column(db.Integer, db.ForeignKey('tg_channels.id'))
    vk_group_id = db.Column(db.Integer, db.ForeignKey('vk_groups.id'))
    
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
    
    publish_to_max = db.Column(db.Boolean, default=False)
    
    last_guid = db.Column(db.String(512))
    is_active = db.Column(db.Boolean, default=True)
    
    user = db.relationship('User', backref=db.backref('rss_sources', lazy=True))