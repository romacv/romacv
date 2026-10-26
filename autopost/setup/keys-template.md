# Файлы ключей владельца

Создать вне репозитория в `~/Documents/keys/autopost/`: каталог 700, обычные файлы владельца 600, без symlinks. Значения не помещать в чат, аргументы команд или историю shell. JSON-ключи сервисных аккаунтов не нужны.

| Файл | Содержимое |
| --- | --- |
| telegram_bot_token.txt | Токен отдельного бота из BotFather |
| telegram_bot_id.txt | Числовой ID бота, сверенный через getMe |
| webhook_secret.txt | Стабильный случайный секрет: 1–256 символов A–Z, a–z, 0–9, `_`, `-` |
| roman_user_id.txt | Числовой Telegram ID владельца |
| roman_chat_id.txt | Числовой ID личного чата |
| channels.json | Объект `{"tg-channel": <id>}`: ID публичного канала @romacv777 «Канал им. Ресенчука Романа», где бот имеет Post messages |
| instagram.json | access_token, user_id, issued_at, expires_at; Instagram Login Creator |
| threads.json | access_token, user_id, issued_at, expires_at |
| github_pat.txt | Fine-grained PAT только romacv/romacv, Contents read/write |
| github_noreply.txt | Адрес автора commit с окончанием @users.noreply.github.com |
| anthropic_key.txt | Ключ workspace; prepaid, auto-reload off, месячный limit |
| webhook_url.txt | https://us-central1-resromcom-493b4.cloudfunctions.net/webhook; сверить после deploy, адрес закреплён также в /u/ |
| worker_url.txt | Фактический HTTPS URL приватного worker; сверить после deploy |

Текстовые файлы содержат одно значение. JSON содержит только перечисленные поля; user_id — строка, issued_at/expires_at — числовые Unix seconds. Скрипт добавляет runtime_service_account, bucket=resromcom-493b4-autopost, timezone=UTC и speech_language=ru-RU. Telegram Business connection хранится в Firestore; подключить Manage stories. Обновлённые Meta tokens из Firestore имеют приоритет над исходными файлами.

Явный `setup.sh secret` всегда добавляет версию из этих файлов, не читая старый секрет; владельцу нужно право добавления версии, чтение прежнего значения не требуется. Затем redeploy webhook, worker и daily, проверить привязки и вручную уничтожить прежнюю неиспользуемую версию в [AUTOPOST → Versions](https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4). Free tier — 6 активных версий. Автоматической очистки нет; подробности в [reference.md](reference.md).
