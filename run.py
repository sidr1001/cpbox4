# run.py
from app import create_app, db
from flask_migrate import Migrate

# Создаем экземпляр приложения, используя нашу "фабрику"
app = create_app()

# Инициализируем Flask-Migrate для управления изменениями схемы БД
migrate = Migrate(app, db)

if __name__ == '__main__':
    # Включаем debug=True только для разработки!
    # Он перезагружает сервер при изменениях и показывает подробные ошибки.
    # В "боевом" режиме (production) его нужно выключить (debug=False).
    app.run(debug=True, host='127.0.0.1', port=8099)