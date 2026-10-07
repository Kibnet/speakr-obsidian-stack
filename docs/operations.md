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

Исторические `ApproveBacklog`/`backlog-approved.json` не являются интерфейсом нового runtime. Непустой `legacy_recovery_jobs` блокирует обычный запуск; переход выполняется явно по процедуре ниже. Удаление этого поля вручную не переносит прежние запреты.

## Переход со старой установки

Adoption переводит старую работающую автоматизацию на те же модули, что находятся в этом репозитории. Очередь, staging, источники, vault и записи остаются на месте; образы Speakr/ASR и модели не обновляются. Поддерживается известный старый layout `TARGET\automation` и отдельный launcher `TARGET\llm\start-ollama.ps1`. Неизвестные action, principal, executable-модули, схема очереди или launcher требуют разбора: инструмент откажется принимать их автоматически.

Выполняйте команды из проверенного checkout, под тем же пользователем Windows, который владеет задачами. Сначала проверьте переход на приватной синтетической копии старого кода:

```powershell
powershell.exe -NoProfile -File tests/validate-adoption.ps1 -LegacyRoot C:\LocalTranscription\automation -Port 19385
```

Fixture копирует только executable-модули старой установки и launcher; пользовательские аудио, очередь, токены и заметки не копируются. Создаёт отдельные уникально названные задачи и синтетический сервер без GPU, проверяет crash/rollback, исторические запреты, PowerShell 5 stderr, изменение definition работающей задачи Ollama и последующий Upgrade. Сохранённые fixture/plan могут содержать личные пути — не коммитьте их.

```powershell
powershell.exe -NoProfile -File scripts/adopt.ps1 -Action Inspect -Root C:\LocalTranscription\automation -Plan C:\LocalConfig\adoption-plan.json
powershell.exe -NoProfile -File scripts/adopt.ps1 -Action Apply -Root C:\LocalTranscription\automation -Plan C:\LocalConfig\adoption-plan.json
powershell.exe -NoProfile -File scripts/adopt.ps1 -Action Verify -Root C:\LocalTranscription\automation -Plan C:\LocalConfig\adoption-plan.json
```

Inspect не изменяет установку. Приватный plan содержит байты старого config/code и task XML: храните вне публичного Git, с правами того же пользователя. Apply проверяет drift, включает maintenance, временно отключает Bridge/Watchdog triggers, блокирует новые старые запуски и ждёт штатного завершения загруженных процессов. Через 45 секунд занятость означает отказ без принудительного завершения. Задача и сервер Ollama продолжают работать; definition меняется для будущего запуска без рестарта. Настройки источников, ACR timestamps, числа собеседников и Ollama tuning сохраняются.

Не запускайте ручные команды установки параллельно с Apply. `--config` с альтернативными конфигами, прямые Python imports и сторонние entrypoints во время перехода не поддерживаются. Обнаруженный сторонний writer блокирует переход. До замены code создаются резервные копии в `automation\backups\adoption-*`; SQLite backup проверяется через integrity_check. `adoption-state.json` хранит фазу и ожидаемые изменения. Отсутствующий config во время перехода — предусмотренный устойчивый к crash барьер: не создавайте его вручную из backup.

Неразрешённые исторические job ID переносятся в `recovery_holds`. Запрет действует также через другие jobs с тем же remote recording ID. Такие записи требуют отдельной сверки идентичности; retry/re-export/backfill не снимают запрет. Переход не разрешает новую обработку старого архива, не восстанавливает удалённые заметки и не откатывает существующие таблицы очереди.

Успешный Apply оставляет maintenance pause. Если установка была активна до перехода, запустите проверяемый Resume:

```powershell
powershell.exe -NoProfile -File C:\LocalTranscription\automation\recovery-control.ps1 -Action Resume
```

Если установка прежде была paused или tasks были disabled, сохраните этот выбор; Apply сохраняет исходные Enabled flags. Resume сверяет code/config/task ownership и не запускает disabled задачи. Проверьте свежий heartbeat, health и совпадение файлов активного runtime с checkout. `install-state.json.commit` — commit пакета adoption; полный manifest хешей определяет фактически установленный код. После Upgrade ориентируйтесь на обновлённые file hashes; версия сервисов проверяется отдельно. Повторный Apply того же пакета только проверяет установленную версию; другой пакет обновляется штатным `scripts/install.ps1 -Action Upgrade -TargetRoot TARGET`.

При прерывании сохраняйте паузу и используйте тот же проверенный пакет:

```powershell
powershell.exe -NoProfile -File scripts/adopt.ps1 -Action Rollback -Root C:\LocalTranscription\automation -Plan C:\LocalConfig\adoption-plan.json
```

Rollback до изменений сверяет принадлежащие переходу code/config/tasks и логические данные. Восстанавливает прежние модули, config и task XML, удаляет только собственные migration holds/новые модули. SQLite и заметки не восстанавливаются из старого backup. Если данные изменились после Resume или есть чужой drift, автоматический rollback отказывает: нужен отдельный reconciliation, без потери новых записей. После успешного rollback запуск старого Bridge выполняется явно; Ollama не перезапускается. Старые `automation\install.ps1` и `start.ps1` после adoption заменены публичными совместимыми entrypoints: старый installer отказывает, start вызывает проверяемый Resume.

Сохранённые triggers и native task/launcher fixture подтверждают конфигурацию автозапуска. Реальная перезагрузка ПК — отдельная проверка; её нельзя считать выполненной по одному успешному Resume.
