# Файлы ключей владельца

Создать вне репозитория в `~/Documents/keys/autopost/`: каталог 700, обычные файлы владельца 600, без symlinks. Значения не помещать в чат, аргументы команд или историю shell. JSON-ключи сервисных аккаунтов не нужны.

`setup.sh ids` до регистрации webhook: после /start боту записывает Telegram ID и channels.json, проверяет права Post/Edit/Delete messages; существующие отличающиеся значения не заменяет.

`setup.sh meta` читает только присутствующие `threads_token.txt` / `instagram_token.txt`, проверяет @romacv777 через официальный /me и записывает JSON атомарно с mode 600. Значения не печатаются. Существующий JSON с другим access_token не заменяется: для замены сначала удалить его и повторить `meta`. После проверки сохранённого access_token исходный текстовый файл удаляется. Остальные ключи команда не читает.

Threads: приложение **resrom threads → User Token Generator**, выдать `threads_basic`, `threads_content_publish`, `threads_delete` и сохранить токен в `threads_token.txt`. Команда проверяет issued_at/expires_at через документированный [debug_token](https://developers.facebook.com/documentation/threads/troubleshooting/debug-access-token). Для короткого токена требуется `threads_app_secret.txt`; при отсутствии команда назовёт файл и остановится. [Обмен](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens) возвращает новый токен и expires_in. Долгоживущий токен сохраняется без refresh, включая первые 24 часа.

Instagram: приложение **resrom posting → Manage messaging & content on Instagram → API setup with Instagram login → Generate token** для **@romacv777**, сохранить в `instagram_token.txt` и сразу выполнить `setup.sh meta`. [Токен из App Dashboard долгоживущий и действует 60 дней](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started). Для нового JSON команда записывает issued_at = текущее время и expires_at = issued_at + 60 суток, исходя из запуска сразу после генерации; это расчёт по документированному сроку, а не introspection. При повторном запуске с тем же токеном в существующем JSON исходные времена сохраняются. Короткие токены из OAuth flow для этого входа не подходят. [access_token](https://developers.facebook.com/docs/instagram-platform/reference/access_token) документирует их обмен с Instagram App Secret, [refresh_access_token](https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token) — обновление только долгоживущего токена не моложе 24 часов, ещё не истёкшего. Setup не вызывает refresh: позже его выполняет daily по этим условиям. Дополнительный файл метаданных не нужен.

JSON содержит ровно четыре поля, без scopes. Для правки/удаления Threads отдельно записать фактически выданные `threads_basic`, `threads_content_publish`, `threads_delete` в Firestore `tokens/threads.scopes` вместе с текущими полями токена; `meta` не пишет в Firestore. Refresh сохраняет имеющиеся scopes, но не добавляет разрешения.

| Файл | Содержимое |
| --- | --- |
| telegram_bot_token.txt | Токен отдельного бота из BotFather |
| telegram_bot_id.txt | Числовой ID бота, сверенный через getMe |
| webhook_secret.txt | Стабильный случайный секрет: 1–256 символов A–Z, a–z, 0–9, `_`, `-` |
| roman_user_id.txt | Числовой Telegram ID владельца |
| roman_chat_id.txt | Числовой ID личного чата |
| channels.json | Объект `{"tg-channel": <id>}`: ID публичного канала @romacv777 «Канал им. Ресенчука Романа», где бот имеет Post messages |
| threads_token.txt | Одно значение из User Token Generator; вход для `meta` |
| instagram_token.txt | Только свежий долгоживущий токен из Generate token с Instagram Login; сразу выполнить `meta` |
| threads_app_secret.txt | Threads App Secret; читается `meta` только для обмена короткого токена |
| instagram.json | access_token, user_id, issued_at, expires_at; Instagram Login Creator |
| threads.json | access_token, user_id, issued_at, expires_at |
| github_pat.txt | Fine-grained PAT только romacv/romacv, Contents read/write |
| github_noreply.txt | Адрес автора commit с окончанием @users.noreply.github.com |
| anthropic_key.txt | Ключ workspace; prepaid, auto-reload off, месячный limit |
| webhook_url.txt | https://us-central1-resromcom-493b4.cloudfunctions.net/webhook; сверить после deploy, адрес закреплён также в /u/ |
| worker_url.txt | Фактический HTTPS URL приватного worker; сверить после deploy |

Текстовые файлы содержат одно значение. JSON содержит только перечисленные поля; user_id — строка, issued_at/expires_at — числовые Unix seconds. Скрипт добавляет runtime_service_account, bucket=resromcom-493b4-autopost, timezone=Asia/Makassar и speech_language=ru-RU с альтернативным en-US. Telegram Business connection хранится в Firestore; подключить Manage stories. Обновлённые Meta tokens из Firestore имеют приоритет над исходными файлами.

Явный `setup.sh secret` всегда добавляет версию из этих файлов, не читая старый секрет; владельцу нужно право добавления версии, чтение прежнего значения не требуется. Затем redeploy webhook, worker и daily, проверить привязки и вручную уничтожить прежнюю неиспользуемую версию в [AUTOPOST → Versions](https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4). Free tier — 6 активных версий. Автоматической очистки нет; подробности в [reference.md](reference.md).
