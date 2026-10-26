# Настройка владельцем

Проект `resromcom-493b4`, регион `us-central1`. Нужны Python 3, gcloud и вход владельца. `setup.sh` работает из любого каталога. Права deploy/build, включения API, управления IAM и billing остаются у владельца. Сервисные аккаунты Google сохраняют штатные service-agent роли; Cloud Tasks service agent должен выпускать OIDC-токены.

`apis` включает API; `infra` создаёт Firestore Native, приватный bucket, очередь worker, budget-alerts и AUTOPOST. Очередь: одна попытка и один параллельный вызов. Bucket: uniform access, public access prevention, lifecycle 2 дня, soft delete отключён (0), PUT CORS только с https://www.resrom.com. Несовместимый регион или тип базы останавливает настройку. Клиентские rules deny all нужно отдельно выложить; signed URLs используют IAM.

`render` собирает и выкладывает job, но не запускает его: 2 CPU, 2 GiB, 1800 с, один task, без retries. Память включает временные файлы и ffmpeg; пакет ограничен 1 GiB, фактическое потребление проверяется на типичных материалах. После первого deploy измерить суммарный размер `gcf-artifacts` и `cloud-run-source-deploy`: бесплатные 0.5 GB могут быть превышены. `infra` заменяет cleanup policies этих репозиториев удалением образов старше суток, включая tagged; это не ограничение размера и сокращает возможность rollback. Storage lifecycle асинхронен; ранее soft-deleted объекты сохраняются до конца прежнего срока.

## Выдаваемые права

| Кому | Область | Роль и причина |
| --- | --- | --- |
| runtime и render | Проект | `roles/datastore.user`: документы, запросы и транзакции состояния |
| runtime и render | Только bucket resromcom-493b4-autopost | `roles/storage.objectAdmin`: чтение исходников и поколений, запись результатов, удаление объектов |
| runtime и render | Только очередь worker | `roles/cloudtasks.enqueuer`: постановка заданий и доставка результатов job |
| runtime и render | Только runtime SA | `roles/iam.serviceAccountUser`: actAs для OIDC задач от runtime SA |
| runtime | Только runtime SA | custom `autopostSignBlob`: `iam.serviceAccounts.signBlob` для подписанных PUT/GET |
| runtime | Только worker Cloud Run service | `roles/run.invoker`: вызов приватного worker через Tasks OIDC |
| runtime | Только autopost-render job | custom `autopostRunJob`: `run.jobs.run` и `run.jobs.runWithOverrides` для prepare/render с параметрами |
| runtime | Только секрет AUTOPOST | `roles/secretmanager.secretAccessor`: чтение привязанной конфигурации |
| budget | Проект | custom `autopostBudget`: `resourcemanager.projects.get` для getBillingInfo, `resourcemanager.projects.deleteBillingAssignment` для отключения billing, `logging.logEntries.create` для журналирования |

`iam` снимает старые runtime bindings и удаляет custom roles `autopostMaintenance`, `autopostSecretMaintenance`, `autopostSpeech`. Прочие ранее выданные роли не снимаются: владелец проверяет отсутствие широких grants. Привязки worker/job откладываются до создания ресурсов; затем повторить `iam`. Render не читает AUTOPOST; overrides сохраняют настроенный render SA. Budget права изолированы от runtime/render и нужны обработчику после включения реального отключения.

`media.py` использует Speech-to-Text **V1** `long_running_recognize` и опрос операции. Официальные контракты [longrunningrecognize](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v1/speech/longrunningrecognize) и [operations.get](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v1/operations/get) требуют OAuth scope `cloud-platform` и не указывают отдельных Speech IAM permissions. Для этих вызовов отдельная Speech role не выдаётся: API включён, используются credentials прикреплённого SA и уже выданный доступ к GCS. Не переносить требования V2 recognizers на V1.

## Секреты и обслуживание

`secret` запускается владельцем только для передачи конфигурации из файлов по [шаблону](keys-template.md). Каждый явный запуск добавляет новую версию, даже при одинаковых данных; существующее значение никогда не читается. JSON передаётся через stdin в [gcloud secrets versions add](https://docs.cloud.google.com/sdk/gcloud/reference/secrets/versions/add), без аргументов со значениями и временного файла. Логи gcloud и core dumps отключены; вывод ошибок скрыт.

После обновления выполнить redeploy webhook, worker и daily, проверить их привязки и работу. Затем вручную уничтожить прежнюю неиспользуемую версию: [AUTOPOST → Versions](https://console.cloud.google.com/security/secret-manager/secret/AUTOPOST/versions?project=resromcom-493b4), Actions → Destroy. Не удалять версии, ещё используемые deployments. Free tier — 6 активных версий; автоматической очистки нет. См. [Firebase secrets](https://firebase.google.com/docs/functions/config-env?gen=2nd), [удаление версии](https://docs.cloud.google.com/secret-manager/docs/destroy-secret-version), [тарифы](https://cloud.google.com/secret-manager/pricing).

`webhook` повторно применяет Telegram secret_token и allowed_updates без сброса pending updates. `budget-test` публикует [пример Google](https://docs.cloud.google.com/billing/docs/how-to/listen-to-notifications#test_a_cloud_run_function) один раз по локальной квитанции; возможна повторная доставка при сбое. Проверить deployed `SIMULATE_DEACTIVATION=True` и журнал «Billing disabled. (Simulated)». Kill switch остаётся в симуляции до drill в отдельном проекте; живое отключение включается отдельно. Уведомление бюджета запаздывает и не является денежным cap: [Google sample](https://docs.cloud.google.com/billing/docs/how-to/disable-billing-with-notifications).
