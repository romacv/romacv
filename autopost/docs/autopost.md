# Автопостер на Firebase

Роман отправляет боту текст, фото, голос или видео. Бот готовит черновик и финальные файлы, показывает их в личном чате и ждёт «Опубликовать». Подтверждение привязано к конкретным текстам и байтам медиа. «Править» создаёт новую версию: следующее текстовое сообщение становится правкой, ответ не нужен. «Отклонить» закрывает черновик. Ответ (в том числе с цитатой) на сообщение превью правит только эту цель. Каждый ответ бота называет принятый материал и черновик.

| Часть | Назначение |
| --- | --- |
| webhook | Functions 2nd gen, Python 3.12, us-central1; проверяет Telegram secret, владельца и личный чат; сохраняет update и outbox транзакцией |
| worker | 2 GiB, 540 с, одна попытка и один параллельный вызов; черновики, превью, публикация и приём результатов job |
| autopost-render | Приватный Cloud Run job: подготовка аудио, кадров и контактных листов, затем отдельный финальный монтаж; 2 CPU, 2 GiB, 1800 с, без retries |
| Firestore Native | Состояния, версии, checkpoints, tokens и outbox; us-central1, клиентские rules deny all |
| Storage | Приватный resromcom-493b4-autopost: inbox/ и media/, поколения объектов и SHA-256, lifecycle 2 дня |
| daily | Каждые 15 минут восстанавливает зависшие публикации; обслуживание выполняет раз в день в 06:00 выбранной зоны, по умолчанию Asia/Makassar |
| budget_kill | Отдельный codebase и SA; бюджет $1 отвязывает billing |

Код расположен в `autopost/functions/`; промпт — `autopost/functions/draft-agent.md`, накопленные правила — `autopost/functions/EXPERIENCE.md`. Черновик создаёт Anthropic с одной коррекцией JSON и локальной валидацией. Speech-to-Text V1 возвращает слова с таймкодами; резервирование в Firestore останавливает распознавание на 55 мин/месяц.

Альбом собирается с задержкой **10 с**; revision делает устаревшие задания неактуальными. Видео накапливаются до кнопки «Смонтировать» под ответом бота; «Отменить» сбрасывает пакет. Пакет ограничен **1 GiB и 20 файлами**, отдельный MP4 — 512 MiB. Для видео больше 20 MiB бот выдаёт ссылку /u/ на 15 минут; /upload обновляет ссылку ожидающего пакета. Файлы выбираются по порядку списка. HMAC билета отделён от webhook secret, квитанция проверяет Origin, срок и revision.

Страница /u/ закрепляет endpoint `https://us-central1-resromcom-493b4.cloudfunctions.net/webhook` и bucket `https://storage.googleapis.com/resromcom-493b4-autopost/` в проверках и CSP. Произвольные run.app и чужие buckets не допускаются; после deploy сверить адрес webhook с этой настройкой.

Подготовка аудио/видео выполняется в Cloud Run: worker передаёт manifests объектов, а не весь пакет в памяти. Job извлекает FLAC, длительности, кадры раз в секунду и до 100 контактных листов. Монтаж проверяет источники, конечные числовые времена, границы клипов и субтитров. Каждый итоговый файл очищается от метаданных перед хешированием. Reel/канал/Threads — до 90 с, stories — до 60 с. Финальный render делает crop/blur pad, loudnorm, встроенные субтитры и необязательные заголовки места с пином (до 20 на таргет, 28 символов); Reel — H.264/AAC 1080×1920, Telegram story — H.265/AAC 720×1280 до 30 MB. Обложка входит в длительность. Перед подтверждением нужно проверить изображение и звук.

Планирование `at` ограничено **36 часами** от проверки; lifecycle не гарантирует наличие материала после долгого ожидания подтверждения. При отсутствии объекта бот сообщает «Материал истёк. Пришли файлы заново.»; при загрузке возможен вариант «Материал истёк или не загружен. Пришли файлы заново.».

Состояния: drafting → previewed → approved → running → done/partial/failed. Истёкший материал завершает публикацию в failed без кнопки повтора. Правка снимает старое подтверждение; повторные update/callback не публикуют материал снова. Перед внешним действием сохраняется pending checkpoint, после ответа — done с ID. Явный HTTP 4xx помечает попытку failed; сетевой сбой оставляет pending до ручной сверки результата у платформы. «Повторить» из partial пропускает готовые шаги и не снимает pending. Контейнеры Meta ERROR/EXPIRED пересоздаются без сброса успешного publish checkpoint. Blog после PATCH 422 сверяет текущую ссылку ветки.

Каждый **15-минутный tick** переводит running старше 10 минут в partial и предлагает повтор с сохранёнными результатами. Ежедневное обслуживание также восстанавливает недоставленные результаты prepare/render. Подписанные GET привязаны к generation и действуют 60 минут; проверяются полным GET, Content-Type, длиной и SHA-256. Неудачная проверка публичной ссылки не вызывает повторную публикацию.

Рабочие и личные посты Telegram публикуются по-русски в одном публичном канале @romacv777 «Канал им. Ресенчука Романа» (`tg-channel`). Отчёт содержит ссылки `https://t.me/romacv777/<message_id>`; рабочие Instagram/Threads и blog — по-английски, личные тексты — по-русски. Blog использует атомарный Git Data commit в main от Roman Resenchuk с GitHub noreply; отчёт сверяет Action по commit SHA и title статьи. Непроверенный URL отмечается явно. Facebook mirror проверяется после Instagram; при отсутствии нужна ручная публикация. Ссылку `ig-story`, вычисленную из shortcode, обязательно открыть на телефоне **при первой живой story**; до этой проверки её корректность не подтверждена.

## Токены, разрешения и тестировщики

В Meta два приложения: **resrom threads** с use case **Access the Threads API** и разрешениями `threads_basic`, `threads_content_publish`, `threads_delete`; **resrom posting** с **Manage messaging & content on Instagram → API setup with Instagram login** и `instagram_business_basic`, `instagram_business_content_publish`. Meta запрещает «instagram» в имени приложения, а Threads нельзя объединить с другими use cases в одном приложении.

Оба приложения остаются в development mode: пользоваться ими могут только аккаунты с ролью в приложении. Тестировщик — сам Роман, аккаунт **@romacv777**, с ролями **Threads Tester** и **Instagram Tester**; других тестировщиков нет. Приглашение нужно принять: Threads — **threads.com → Settings → Website permissions → Invites**, Instagram — **instagram.com/accounts/manage_access → Tester invites**. App Review не нужен: приложения работают только с собственным аккаунтом Романа.

Токен даёт приложению доступ к аккаунту в пределах выданных разрешений. Роль тестировщика разрешает использовать приложение в development mode, но не заменяет разрешения токена.

```mermaid
flowchart TD
    roman["Роман: Generate token в Meta dashboard"] --> raw["~/Documents/keys/autopost: threads_token.txt / instagram_token.txt"]
    raw --> meta["setup.sh meta: проверка username romacv777"]
    meta --> json["threads.json / instagram.json; после проверки записи исходный файл удаляется"]
    json --> secret["setup.sh secret"]
    father["BotFather: токен бота"] --> tg["~/Documents/keys/autopost: telegram_bot_token.txt"]
    tg --> ids["setup.sh ids: после /start, до webhook; ID бота, владельца и канала @romacv777; проверка прав администратора"]
    ids --> secret
    secret --> sm["Secret Manager: AUTOPOST"]
    sm --> fn["Redeploy webhook, worker и daily"]
    fn --> refresh["daily: refresh Meta при остатке меньше 30 суток и возрасте от 24 часов"]
    refresh --> store["Firestore: tokens/threads и tokens/instagram"]
    store --> runtime["Functions: выбирается токен с более поздним временем выдачи"]
    fn --> runtime
```

`meta` не заменяет отличающийся JSON: перед заменой токена удалить соответствующий `threads.json` или `instagram.json`. Короткий Threads token обменивается с `threads_app_secret.txt`; Instagram token нужен свежий из Generate token. Refresh сохраняет имеющиеся scopes, но не выдаёт новые. `meta` сохраняет фактически выданные разрешения из [debug_token data.scopes](https://developers.facebook.com/documentation/threads/troubleshooting/debug-access-token) в `threads.json.scopes`. Runtime использует scopes выбранного токена.

```mermaid
flowchart TD
    owner["Только Telegram ID Романа; личный чат"] --> hook["webhook: проверка владельца, чата и Telegram secret"]
    hook --> worker["worker: черновик и подтверждение публикации или изменения"]
    worker --> channel["Канал @romacv777: бот — администратор; Post / Edit / Delete messages"]
    worker --> story["Личная Telegram story: Business connection Романа; Manage stories"]
    worker --> threads["Threads: threads_basic, threads_content_publish, threads_delete"]
    worker --> instagram["Instagram: instagram_business_basic, instagram_business_content_publish"]
    worker --> github["Blog: GitHub PAT; Contents read/write только romacv/romacv"]
```

Перед действием runtime сверяет личность бота, канал и его права, владельца активного Business connection; для Meta — @romacv777 и сохранённый user_id, для Instagram также тип Creator. Instagram правится и удаляется вручную; разрешения публикации этого не меняют.

Вручную повторить настройку нужно при отзыве токена: например, после смены пароля, удаления приложения или `/revoke` в BotFather. Сгенерировать новый токен, сохранить в соответствующий файл в `~/Documents/keys/autopost`, для Meta выполнить `setup.sh meta` после удаления старого JSON; для Telegram заменить `telegram_bot_token.txt`. Затем `setup.sh secret` и redeploy webhook, worker и daily. Для каждого провайдера runtime и daily выбирают более новый токен: issued_at из AUTOPOST сравнивается с максимумом issued_at/refreshed_at из Firestore. При равенстве выбирается Firestore. Новый AUTOPOST после ручной генерации выигрывает у старого Firestore, последующий refresh снова делает Firestore новее. Ручная правка Firestore не нужна. Daily пропускает refresh истёкшего токена и отправляет Роману строку с провайдером и «нужен новый токен: Generate, setup.sh meta, secret» не чаще одного раза в сутки для каждого провайдера.

## Настройка

Права каждого SA и причины grants перечислены в [reference.md](../setup/reference.md). Runtime читает только AUTOPOST через secretAccessor, без удаления версий и инвентаризации функций. Для подписания выдан только signBlob на собственном SA; для запуска job — run и runWithOverrides на конкретном job. Speech V1 не требует отдельной Speech role: см. официальные [longrunningrecognize](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v1/speech/longrunningrecognize) и [operations.get](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v1/operations/get).

1. Включить Blaze, выполнить `autopost/setup/setup.sh apis`, затем `infra` и `iam`. Подключить существующий bucket к Firebase Storage. Подготовить Telegram Post messages/Manage stories, два Meta-приложения и роли тестировщика, GitHub PAT и Anthropic prepaid с auto-reload off и месячным limit.
2. Создать приватные файлы по [keys-template.md](../setup/keys-template.md). До регистрации webhook отправить боту /start и выполнить `setup.sh ids`: команда заполнит Telegram ID и channels.json, проверив права администратора и Post/Edit/Delete messages в @romacv777. В **resrom threads → User Token Generator** и **resrom posting → Manage messaging & content on Instagram → API setup with Instagram login → Generate token** для @romacv777 получить токены, сохранить в threads_token.txt / instagram_token.txt и выполнить `setup.sh meta`. Команда проверяет личность, не печатает значения и не заменяет отличающийся JSON. Threads проверяется через debug_token; короткий токен требует threads_app_secret.txt. Instagram token должен быть только что получен через Generate token: сразу выполнить `meta`, которая запишет issued_at = now и expires_at = now + документированные 60 суток. Повтор с тем же токеном сохраняет исходные времена. Setup не вызывает refresh; позже daily пытается обновить токен, когда до expires_at осталось меньше 30 суток токен ещё не истёк и с issued_at/refreshed_at прошли хотя бы 24 часа. Подробности и официальные ссылки — в шаблоне ключей. `meta` сохраняет data.scopes из debug_token в threads.json; refresh сохраняет scopes выбранного токена. Явный `setup.sh secret` всегда добавляет версию из переданных владельцем файлов, не читая существующее значение.
3. Выполнить `setup.sh render`; выложить functions, Firestore/Storage rules и Hosting. Проверить приватность worker/job и публичность webhook. Сверить URL, повторить `iam` и `infra` для resource grants и настроек после deploy. После первого deploy измерить суммарный размер Artifact Registry.
4. После каждого изменения AUTOPOST выполнить redeploy webhook, worker и daily, проверить работу и привязанные версии. Вручную уничтожить прежнюю неиспользуемую версию: [AUTOPOST → Versions](https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4), Actions → Destroy. Free tier — **6 активных версий**; автоматической очистки нет. Не уничтожать версии, ещё используемые deployments: [Firebase secrets](https://firebase.google.com/docs/functions/config-env?gen=2nd), [Secret Manager](https://docs.cloud.google.com/secret-manager/docs/destroy-secret-version).
5. Выполнить `setup.sh webhook` с тем же секретом и URL; pending updates сохраняются. Бюджет $0.01 только для писем; бюджет $1 с порогами 50/90/100% и Pub/Sub budget-alerts запускает kill switch.
6. Drill пройден 2026-10-27 с SIMULATE_DEACTIVATION=True («Billing disabled. (Simulated)»); теперь режим боевой. **`setup.sh budget-test` в боевом режиме отключит billing**: только после возврата True и deploy. Отключение billing переводит проект на [Spark](https://firebase.google.com/docs/projects/billing/firebase-pricing-plans): Pub/Sub, Cloud Run, deploy функций и bucket autopost недоступны до возврата Blaze, данные bucket сохраняются; Hosting и Firestore там не описаны. Уведомления запаздывают и не ограничивают расходы: [Google sample](https://docs.cloud.google.com/billing/docs/how-to/disable-billing-with-notifications).

Первый запуск проверять последовательно с отдельным подтверждением Романа: Telegram text/photo/album/story, Threads, Instagram post/story/Reel и Facebook mirror, blog, большой MP4 через /u/, кнопки «Смонтировать» и «Отменить», правки, отклонение и несколько целей. Проверить доступность медиа для Meta, конечные кодеки, длительность, звук и все ссылки на телефоне.

Free tier не гарантирует нулевой счёт. Scheduler: 3 бесплатных задания на billing account. Cloud Build оставляет исходники в staging bucket: после первого deploy проверить его и удалить staged sources. Artifact Registry: превышение 0.5 GB суммарно вероятно; cleanup старше суток не является лимитом размера. Secret Manager: 6 активных версий; Speech V1 Standard: первые 60 минут, приложение резервирует до 55. Soft delete bucket отключён (0). Render, builds, egress и другие workloads billing account учитываются отдельно. После первого deploy и живого запуска проверить размер образов и Billing report: [free tier](https://docs.cloud.google.com/free/docs/free-cloud-features), [Cloud Run](https://cloud.google.com/run/pricing), [Speech](https://cloud.google.com/speech-to-text/pricing), [Secret Manager](https://cloud.google.com/secret-manager/pricing).

## Правка и удаление опубликованного

Под отчётом о публикации кнопки «Править» (следующее текстовое сообщение становится правкой) и «Удалить»; ответ текстом на сообщение опубликованного поста тоже правка. Слова в тексте командами не считаются. Бот показывает изменённые строки или список целей удаления и ждёт отдельного подтверждения с хешем. Канал требует дополнительно Edit messages и Delete messages; альбом правится по подписи первого сообщения и удаляется целиком, а старые альбомы без всех ID удаляются вручную. Business story правится с исходным медиа (при истёкшем Storage потребуется ручная правка), Threads правится через удаление и повторную публикацию: превью предупреждает, что лайки и комментарии пропадут. Нужен новый токен со scopes threads_basic, threads_content_publish, threads_delete и их список в scopes выбранного токена; refresh не добавляет разрешения. Перед удалением проверяется [threads_publishing_limit](https://developers.facebook.com/docs/threads/troubleshooting?locale=en_US#retrieve-quota-limits) (delete_quota_usage/delete_config), максимум 100 за 24 часа. Исходное медиа проверяется до удаления; истёкший материал нужно прислать заново. Новый ID и ссылка сохраняются. Instagram caption edit и DELETE не поддерживаются официальным API с Instagram Login: [IG Media](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media). Правка и удаление выполняются вручную по ссылке. Blog меняется одним атомарным Git Data commit; удаление убирает статью из индекса, RSS и sitemap. Отчёт содержит результат и ссылку каждой цели. Повтор пропускает готовые цели; pending требует ручной сверки.

Stories в канале недоступны через Bot API и не добавлены в цели. [postStory](https://core.telegram.org/bots/api#poststory), [editStory](https://core.telegram.org/bots/api#editstory) и [deleteStory](https://core.telegram.org/bots/api#deletestory) требуют `business_connection_id`, а не `chat_id`; поддерживается только личная Business story (`tg-story`) с правом `can_manage_stories`. Канальные права Post/Edit/Delete stories для обычных постов не нужны.
