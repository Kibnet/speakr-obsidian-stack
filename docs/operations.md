# Эксплуатация и восстановление

Все примеры предполагают собственный внешний config. Команды `stack.py` выполняются из checkout. Runtime — отдельный каталог: `stack/` (Compose, env, uploads/instance/models) и `automation/` (config, очередь, logs, controls, install journal). Не редактируйте installer-owned файлы без явной миграции: hashes защищают от незаметного перезаписывания.

## Пауза и запуск

```powershell
python scripts/stack.py pause --config C:\LocalConfig\my-speakr\stack.json
python scripts/stack.py resume --config C:\LocalConfig\my-speakr\stack.json
python scripts/stack.py start --config C:\LocalConfig\my-speakr\stack.json
```

Pause останавливает новые действия bridge/watchdog; активная операция может завершиться. Resume не включает задачи, которые пользователь выключил в Scheduler, и не снимает отдельную discovery pause. Start проверяет собственные container identities и запускает остановленные контейнеры. Watchdog использует login/minute triggers, один recovery action за проход, cooldown и ограничение попыток. Он не перезапускает работающий GPU-сервис ради health error и не подменяет image.

`auto_start_docker` по умолчанию false. Watchdog может запустить проверенный Docker Desktop после входа пользователя. Опциональный true включает Docker AutoStart/StartupApproved с журналом intent/applied; неизвестная startup definition или drift блокируют изменение. Чужие настройки Docker сохраняются. Это не доказательство прохождения реальной перезагрузки. Таймаут docker info означает неизвестное состояние и не запускает GUI. При ошибке engine watchdog запускает Docker Desktop только после успешной проверки отсутствия процессов Docker Desktop и backend; непосредственно перед запуском проверяет их повторно. Ошибка проверки процессов блокирует запуск. Уже работающий Docker не перезапускается ради health error.

## Очередь, ручной retry и старые файлы

Для старой записи используйте `enqueue.pyw` из README. Исторический archive не отправляется автоматически при первом запуске. Источники не изменяются. Для уже существующего job ID:

```powershell
python C:\LocalApps\my-speakr\automation\bridge.py --retry JOB_ID
python C:\LocalApps\my-speakr\automation\bridge.py --re-export JOB_ID
```

Сначала проверьте статус и отчёт в runtime. Неизвестный итог POST не отправляется повторно вслепую. При summary failure используется только известный marker, nonempty transcript, exact filename и fingerprint; события блокируют автоматический summarize, поскольку server reprocessing может удалить события. Ошибки авторизации требуют исправления credentials. Ограничения recovery и history window — в исходниках `automation/recovery.py`; несоответствие API требует явной адаптации и тестов.

## Backup

```powershell
python scripts/stack.py backup --config C:\LocalConfig\my-speakr\stack.json
```

Сохраняется online backup **bridge SQLite**, с integrity check. Для полной резервной копии отдельно сохраните credentials/config/manifests и Obsidian vault; Speakr DB копируйте через SQLite backup API **в app container**, либо после остановки собственного app container вместе с его instance/uploads. Не копируйте живую SQLite/WAL с Windows Docker bind mount. Проверяйте восстановление в отдельном runtime. Tokens, manifests и backups не публикуются.

## Upgrade и rollback

Bridge upgrade — явная операция из новой версии checkout:

```powershell
python scripts/stack.py upgrade --config C:\LocalConfig\my-speakr\stack.json
```

Проверяет ownership/code/task drift, сохраняет snapshots и online queue backup, безопасно останавливает свой worker, атомарно заменяет modules. Tasks и discovery/disabled flags сохраняются. После изменения результат paused; выполните проверку и Resume. Одинаковая версия только проверяется. Ошибка возвращает предыдущий код, не перематывая очередь/заметки. Удаление модулей, новый state/API contract или task action требуют явной миграции.

Для возврата к предыдущему bridge code выберите предыдущий Git commit и выполните тот же owned upgrade после проверки совместимости state/schema. **Не восстанавливайте старую очередь поверх новой**. При interrupted upgrade поле `upgradePending` блокирует последующие изменения: сохраните runtime, сверяйте hashes текущих/previous/incoming modules из journal и backup, восстановите code/install-state атомарно; если обнаружен неизвестный drift, оставьте pause и разбирайтесь вручную. Автоматическая миграция неизвестной старой личной установки не поддерживается: разверните новый runtime; не принимайте чужие файлы/tasks за свои.

Полный откат установки bridge:

```powershell
python scripts/stack.py rollback --config C:\LocalConfig\my-speakr\stack.json
```

Проверяет все owned hashes/tasks **до изменений**, ставит pause, ждёт безопасного выхода worker, удаляет только зарегистрированные собой tasks/code. Очередь, заметки, Speakr data и stack остаются; удаление данных — отдельная ручная операция. Если config/task drift обнаружен, откат отказывается, не перезаписывая изменения пользователя. Прерванную first install можно откатить той же командой по persisted journal.

Обновление Speakr/ASR/model dependencies требует review `config/dependencies.json`, build/contract/integration tests и отдельной миграции runtime Compose/env/manifests. Нельзя просто редактировать hash, чтобы обойти отказ. Model tag может измениться: новый digest фиксируется после проверки public registry, версии/лицензии и качества. Создайте новый alias вместо перезаписи чужого. FFmpeg version/url/SHA-256 обновляются вместе из официального source release; `latest` и отключение checksum не допускаются.

## Где искать сбой

1. Проверьте `maintenance.json`, disabled tasks и `status.json`/`watchdog-status.json`.
2. Проверьте own app login, ASR health, dedicated Ollama `/api/tags` и alias identity. Обычный Ollama на другом порту не является configured LLM.
3. Проверьте доступ app container → `host.docker.internal:LLM_PORT`, не открывая listener в LAN.
4. Сравните manifests и task actions. Не удаляйте lock/queue, не переключайте image и не повторяйте неясный POST.
5. Воспроизведите на синтетическом isolated fixture перед исправлением working runtime.

## Прерванная первая подготовка или запуск

Если Prepare оборвался и `stack-install.json` сохранил `phase: prepared` либо неполный список файлов, остальные команды откажутся принимать runtime как установленный. Сохраните его для разбора; подготовьте новый пустой внешний runtime и новое имя проекта. До Up Prepare не создаёт контейнеров или Windows tasks. Не дописывайте ownership journal вручную ради обхода проверки.

Up сохраняет план собственных имён, image IDs, labels, bind mounts и портов **до** создания контейнеров. Если Compose остановился на середине, устраните причину и выполните `python scripts/stack.py start --config C:\LocalConfig\my-speakr\stack.json`. Start сверяет весь план до изменений, запускает остановленные собственные контейнеры и создаёт только отсутствующие. Image/config/container drift требует разбора, а не принятия чужой установки. Состояние `ready` фиксируется после успешной проверки доступа из app к Ollama. Повторный Start сохраняет bytes установленной конфигурации; изменение runtime identities требует явной миграции.

Исторические `ApproveBacklog`/`backlog-approved.json` больше не являются интерфейсом этой основы. Непустой `legacy_recovery_jobs` в старой конфигурации блокирует инициализацию recovery. Сохраните прежний runtime и разверните новый; удаление этого поля ради автоматического запуска старого архива не является миграцией.
