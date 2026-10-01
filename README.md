# Ai-Me

Multi-user Telegram auto-responder system. Manages personal Telegram accounts (Telethon) that automatically reply to clients in watched groups, controlled via admin Telegram bot and web panel.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-production%20ready-success)

## Основные возможности

- **Мультипользовательская система** — каждый пользователь регистрирует свой Telegram-аккаунт через админ-бот, данные изолированы
- **Автоответ по сценарию** — мгновенный ответ клиенту от имени личного аккаунта в рабочих группах
- **Гибкие шаблоны** — настройка вопросов и ответов, приветствий, благодарностей
- **Двухуровневый переключатель** — глобальный (веб-панель) + персональный (Telegram-бот)
- **Веб-админ-панель** — управление системой через браузер (FastAPI, порт 8000)
- **Административный Telegram-бот** — полное управление настройками прямо в Telegram
- **Карточки задач** — уведомления о каждом автоответе с кнопкой «Сделал»
- **Надёжное хранилище** — SQLite (WAL) для серверной работы с конкурентным доступом

## Как работает

```
┌─────────────────────────────────────────────────────────────────┐
│                    КЛИЕНТЫ В TELEGRAM ГРУППАХ                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ сообщения
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              Telethon Userbot (один на пользователя)            │
│         session_5474309181.session  session_1418838162.session  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    telegram/handler.py                          │
│  фильтр → исключённые → no-action → приветствие → благодарность │
│                    → шаблон → ответ → задача                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                  storage/settings.py (SQLite WAL)               │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │  data.db    │  │ credentials/ │  │  .env (ADMIN_BOT_TOKEN)│  │
│  │  16 таблиц  │  │ {uid}.json   │  │                      │   │
│  └─────────────┘  └──────────────┘  └──────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
    ┌──────────────────┐     ┌──────────────────┐
    │  admin_bot/bot.py│     │  web_admin/app.py│
    │  (aiogram 3)     │     │  (FastAPI:8000)  │
    └──────────────────┘     └──────────────────┘
```

## Структура проекта

```
Ai-Me/
├── main.py                  # точка входа, запуск всех сервисов
├── config.py                # загрузка конфигурации из .env
├── requirements.txt         # зависимости Python
│
├── admin_bot/
│   └── bot.py               # административный Telegram-бот (~3400 строк)
│
├── storage/
│   ├── settings.py          # SettingsStore — SQLite + credentials + .env
│   ├── data.db              # база данных SQLite (создаётся автоматически)
│   └── context.py           # контекст сообщений (in-memory)
│
├── credentials/             # файлы credentials пользователей (gitignore)
│   └── {user_id}.json
│
├── telegram/
│   ├── handler.py           # обработка входящих сообщений
│   └── client.py            # подключение Telethon клиентов
│
├── web_admin/
│   ├── app.py               # FastAPI приложение (8 разделов)
│   └── templates/           # Jinja2 шаблоны (Tabler CSS)
│
├── logs/
│   └── app.log              # лог приложения (gitignore)
│
├── session_{user_id}.session # сессии Telethon (gitignore)
└── .env                     # конфигурация (gitignore)
```

## Установка и запуск

### Требования

- **Python 3.10+**
- Telegram-аккаунт для автоответов
- Telegram-бот для админки (создаётся через [@BotFather](https://t.me/BotFather))

### Установка

```bash
# клонировать репозиторий
git clone <repo-url>
cd ReplyHelper

# создать виртуальное окружение
python -m venv .venv

# активировать (Windows)
.venv\Scripts\activate

# активировать (Linux/Mac)
source .venv/bin/activate

# установить зависимости
pip install -r requirements.txt
```

### Запуск

```bash
python main.py
```

При первом запуске:
- Создаётся `storage/data.db` (SQLite)
- Данные из старого `settings.json` автоматически мигрируют (если существует)
- Веб-панель доступна на `http://localhost:8000`

## Настройка .env

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
```

### Обязательные переменные

| Переменная | Описание | Пример |
|---|---|---|
| `ADMIN_BOT_TOKEN` | Токен админ-бота от [@BotFather](https://t.me/BotFather) | `123456:ABC-DEF...` |
| `ADMIN_USER_ID` | Ваш Telegram ID (узнать через [@userinfobot](https://t.me/userinfobot)) | `123456789` |

### Веб-панель

| Переменная | Описание | По умолчанию |
|---|---|---|
| `WEB_ADMIN_LOGIN` | Логин для входа в веб-панель | `admin` |
| `WEB_ADMIN_PASSWORD` | Пароль для входа в веб-панель | `admin` |
| `WEB_SECRET` | Секретный ключ для сессий Flask | `change-me-in-env` |
| `WEB_PORT` | Порт веб-панели | `8000` |

### Поведение системы

| Переменная | Описание | По умолчанию |
|---|---|---|
| `AUTO_REPLY` | Режим работы: `true` — отправлять ответы, `false` — только логировать | `true` |
| `REPLY_DELAY` | Задержка перед отправкой ответа (секунды) | `1` |
| `BATCH_WINDOW` | Окно группировки сообщений от одного клиента (секунды) | `2` |

### Опциональные (для обратной совместимости)

| Переменная | Описание |
|---|---|
| `TELEGRAM_API_ID` | API ID с https://my.telegram.org (не требуется, если пользователи регистрируются через бот) |
| `TELEGRAM_API_HASH` | API Hash с https://my.telegram.org |
| `TELEGRAM_PHONE` | Номер телефона (не требуется) |
| `ALLOWED_CHAT_IDS` | Список ID чатов через запятую (управляется через бот) |

## Конфигурация

После запуска откройте веб-панель: `http://localhost:8000`

**Разделы веб-панели:**

- **Dashboard** — статистика: входящие, автоответы, ошибки, активные пользователи
- **Bot** — подключение токена бота, глобальный переключатель автоответа
- **Standards** — стандартные приветствия, благодарности, no-action фразы
- **Library** — справочник текстов (библиотека)
- **Users** — список пользователей, деактивация, очистка данных
- **Subscribers** — подписчики бота
- **Analytics** — аналитика по периодам
- **Logs** — логи ошибок с возможностью пометить как решённые
- **Settings** — бэкап базы данных, очистка аналитики/логов

**Регистрация пользователя через Telegram-бот:**

1. Откройте админ-бот → `/start`
2. Меню: **🤖 Помощник** → **📱 Войти по номеру телефона**
3. Отправьте номер телефона (контактом или текстом)
4. Введите `API_ID` (число с https://my.telegram.org)
5. Введите `API_HASH` (строка с https://my.telegram.org)
6. Введите код подтверждения **кнопками** (не сообщением — Telegram блокирует)
7. Если включён 2FA: отправьте пароль сообщением (бот удалит его сразу)
8. Сессия сохранена, пользователь активен

## Логи

Логи записываются в `logs/app.log` и выводятся в консоль.

**Уровни логирования:**

- `INFO` — основные события (запуск, подключение клиентов, обработка сообщений)
- `WARNING` — предупреждения (ошибки подключения, пропущенные сообщения)
- `ERROR` — ошибки (исключения, сбои)

**Пример лога:**

```
2026-10-01 12:00:00 main: Application started in Scenario Mode.
2026-10-01 12:00:01 main: User 5474309181: Telethon client connected
2026-10-01 12:00:02 main: Web admin panel: http://0.0.0.0:8000
```

**Очистка логов:**

```bash
# очистить файл лога
> logs/app.log  # Windows
> logs/app.log  # Linux/Mac
```

## Безопасность

**Изоляция данных пользователей:**

- Каждый пользователь хранится в отдельном файле `credentials/{user_id}.json`
- Физическая изоляция: телефон, API ID, API Hash не пересекаются
- Session-файлы Telethon привязаны к `user_id`

**Защита credentials:**

- Файлы `credentials/`, `*.session`, `.env` добавлены в `.gitignore`
- Пароль 2FA удаляется из чата сразу после ввода (антифишинг)
- Код подтверждения вводится **кнопками**, а не сообщением (Telegram блокирует пересланные коды)

**bot_token:**

- Хранится в `.env` (ключ `ADMIN_BOT_TOKEN`), не в базе данных
- Устанавливается через веб-панель или `.env`
- Не логируется и не выводится в консоль

**Веб-панель:**

- Авторизация по логину/паролю (`WEB_ADMIN_LOGIN`, `WEB_ADMIN_PASSWORD`)
- Сессии защищены `WEB_SECRET` (измените значение по умолчанию!)

**Рекомендации:**

- Измените `WEB_ADMIN_PASSWORD` и `WEB_SECRET` перед деплоем
- Не коммитьте `.env`, `credentials/`, `*.session` в репозиторий
- Используйте HTTPS для веб-панели на продакшене

## Перенос на сервер

### Подготовка

1. **Установите Python 3.10+** на сервере
2. **Склонируйте репозиторий:**
   ```bash
   git clone <repo-url>
   cd ReplyHelper
   ```
3. **Создайте виртуальное окружение:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Настройте `.env`:**
   ```bash
   cp .env.example .env
   nano .env  # заполните переменные
   ```

### Запуск через systemd (Linux)

Создайте файл `/etc/systemd/system/ai-me.service`:

```ini
[Unit]
Description=Ai-Me Telegram Auto-Responder
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/Ai-Me
Environment="PATH=/path/to/Ai-Me/.venv/bin"
ExecStart=/path/to/Ai-Me/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-me
sudo systemctl start ai-me
sudo systemctl status ai-me
```

### Запуск через Docker (опционально)

Создайте `Dockerfile`:

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

Запуск:

```bash
docker build -t ai-me .
docker run -d --name ai-me --env-file .env -p 8000:8000 ai-me
```

### Обратный прокси (Nginx)

Для доступа к веб-панели по домену:

```nginx
server {
    listen 80;
    server_name admin.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Добавьте HTTPS через Let's Encrypt:

```bash
sudo certbot --nginx -d admin.yourdomain.com
```

## Troubleshooting

### Бот не отвечает в группах

**Проблема:** Клиенты пишут в группу, но автоответ не работает.

**Решение:**

1. Проверьте, что пользователь добавил группу в watchlist через Telegram-бот
2. Убедитесь, что двухуровневый переключатель включён:
   - Глобально: веб-панель → **Bot** → **Помощник включён**
   - Персонально: Telegram-бот → **🤖 Помощник** → **Включить автоответ**
3. Проверьте логи: `tail -f logs/app.log`

### Ошибка подключения Telethon

**Проблема:** `Telethon initial connect warning: <error>`

**Решение:**

1. Проверьте `API_ID` и `API_HASH` пользователя в `credentials/{user_id}.json`
2. Убедитесь, что session-файл `session_{user_id}.session` существует
3. Попробуйте перерегистрировать пользователя через Telegram-бот:
   - Меню: **🤖 Помощник** → **⚙️ Настройки** → **🗑 Удалить аккаунт**
   - Затем заново: **📱 Войти по номеру телефона**

### Веб-панель не открывается

**Проблема:** `http://localhost:8000` не отвечает.

**Решение:**

1. Проверьте, что процесс запущен: `ps aux | grep main.py`
2. Проверьте порт: `netstat -tulpn | grep 8000`
3. Проверьте логи: `tail -f logs/app.log`
4. Убедитесь, что `WEB_PORT` в `.env` совпадает с используемым портом

### Миграция из settings.json не произошла

**Проблема:** Данные из старого `settings.json` не перенеслись в SQLite.

**Решение:**

1. Убедитесь, что `storage/settings.json` существует
2. Удалите `storage/data.db` (если создан пустой):
   ```bash
   rm storage/data.db
   ```
3. Перезапустите приложение — миграция выполнится автоматически

### Код подтверждения не принимается

**Проблема:** Telegram-бот не принимает код, введённый кнопками.

**Решение:**

1. Убедитесь, что код вводится **кнопками**, а не сообщением (Telegram блокирует текстовые коды)
2. Если код истёк: нажмите **🔄 Запросить код повторно**
3. Проверьте, что `API_ID` и `API_HASH` корректны (с https://my.telegram.org)

### Пароль 2FA не принимается

**Проблема:** Бот сообщает «Пароль не принят».

**Решение:**

1. Убедитесь, что пароль вводится правильно (учитывайте регистр)
2. Проверьте, что 2FA включён на аккаунте (Telegram → Настройки → Конфиденциальность → Облачный пароль)
3. Попробуйте ввести пароль заново — бот удалит сообщение сразу

---

**Лицензия:** MIT

**Поддержка:** создайте Issue в репозитории
