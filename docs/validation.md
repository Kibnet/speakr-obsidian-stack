# Проверки и их границы

## Adoption существующей автоматизации — 7 октября 2026 года

Переход проверен отдельно от новой установки и обновления образов:

| Проверка | Результат |
| --- | --- |
| Python bridge/recovery/watchdog/deployment/adoption regressions | 124 tests PASS, Python 3.14 |
| Исторические holds, remote-ID aliases, новое аудио после held job | PASS; запрет не обходится и не блокирует следующую разрешённую запись |
| Atomic DDL/inserts, metadata bytes, missing/drifted manifests, holds и внешние inputs | PASS |
| Native Task Scheduler: 8 Apply crash boundaries | PASS в нескольких прогонах; очередь, config и заметки сохранены |
| Native обрыв самого Rollback после pause и после восстановления tasks | PASS; повторный Rollback завершает переход |
| Running task definition update, retained server PID, Resume и adopted Upgrade | PASS на синтетическом сервере |
| PowerShell 5: listener present/confirmed-empty/query-error, native stderr | PASS |
| Process drain: transient CIM retry, active writer, persistent unknown | PASS; неизвестное состояние не разрешает менять DB/code |
| Native обычная установка: collision, partial/crash rollback, повторный Resume, Upgrade, disabled flags, queue/config drift и logon/minute triggers | PASS; отдельный чистый root и задачи, рабочая установка не изменялась |

В проверочном Windows-сеансе cold scheduled PowerShell startup иногда задерживался до первой строки launcher; причина не установлена. Для проверки перехода уже работающего сервера использован диагностический `tests/validate-adoption.ps1 -BootstrapServer`: синтетический сервер запускается собственной задачей, её definition возвращается к старому launcher при работающем экземпляре, затем проверяются adoption/rollback/Resume/Upgrade и сохранение PID. Эта проверка не подтверждает cold scheduled PowerShell startup, вход в Windows или физическую перезагрузку. Launcher отдельно выполнен в PowerShell 5 с настоящим синтетическим executable и stderr.

Обычный режим `tests/validate-adoption.ps1` проверяет запуск старого launcher через Task Scheduler; `-Boundaries` позволяет повторять выбранные crash checkpoints. Диагностический bootstrap не следует считать заменой проверки автозапуска конкретной машины. Реальная перезагрузка, новая пользовательская запись и phone sync остаются отдельными шагами приёмки.

Проверено 6 октября 2026 года на Windows. Репозиторий является основой развёртывания; результат синтетических проверок не заменяет приёмку вашей установки.

| Проверка | Фактический результат |
| --- | --- |
| Standalone bridge/recovery/watchdog + deployment regressions | 105 tests PASS на Python 3.14 |
| Та же suite на Python 3.11 в контейнере | 105 tests, PASS; 1 Git archive test пропущен из-за отсутствия Git в runtime image, на host он PASS |
| PowerShell 5.1 parsing всех scripts | PASS |
| Native first install/collision/7 partial failures/crash rollback/Resume | PASS; actual Task Scheduler, own temporary roots/tasks |
| Upgrade failure rollback/disabled flags/queue/config drift/interrupted Upgrade Resume | PASS |
| Docker startup registry intent/drift/rollback | PASS; isolated HKCU key, рабочие настройки Docker не менялись |
| Cold build public pinned Speakr + durable FFmpeg source | PASS; FFmpeg/ffprobe 8.1.3, затем final build с 78 Python constraints |
| Actual built Speakr + synthetic ASR/LLM → ready note | PASS; auth/jobs/events, неизменность заметки |
| Empty Prepare → interrupted Up → fresh-process Start → normal native Install → Start → Resume → note | PASS |
| Actual Speakr summary/chat negative requests + proxy/egress | PASS: 10 actual summary/chat negative calls, 0 external DNS/connect, 0 proxy forwarded requests |
| Fresh staged Git export standalone suite/native smoke | PASS: 105 tests + native install/crash/upgrade/rollback suite |
| Staged privacy/ignore/license audit | PASS: нейтральные inputs, AGPL-3.0; runtime, secrets, audio, private audit исключены |
| Новая установка настоящих ASR/Ollama моделей на GPU | не выполнялась здесь |
| Реальная перезагрузка Windows и phone sync | не выполнялись здесь |

Python **3.11+**: проверены 3.11 и 3.14; bridge использует stdlib. Docker image использует закреплённый Python 3.11 base. Reference profile соответствует ранее настроенному Windows/RTX 5070 Ti стеку, но не доказывает свежий full-GPU deploy из этого repository. Synthetic ASR/LLM проверяют реальные app API/auth/jobs и deployment wiring, а не качество моделей, diarization или скорость GPU. Ваша первая реальная запись и перезагрузка остаются шагами приёмки конкретной установки.

Native tests используют actual Task Scheduler и временные roots; registry test использует отдельный ключ. Container integration использует unique names/network/ports/data и не получает GPU. Combined намеренно заменяет только ASR/LLM синтетическими сервисами, записывает fixture model metadata и отключает GPU reservation; настоящий app собирается из опубликованного source commit. Cold build выполнялся без cache; последующие исправления локальной policy/constraints проверены финальной сборкой. Это не утверждение, что самый последний image собран без cache.

App policy ограничивает Python DNS/socket destinations и очищает proxy environment. Это прикладная защита локального processing, а не общая сетевая sandbox для всех контейнеров: downloads ASR/model setup и другие процессы требуют отдельной сетевой политики при необходимости.

Подробные logs, temporary roots и fixture transcripts сохраняются локально в ignored `.validation/` и временных каталогах; в Git входят тесты и этот итог. Эти проверки не включали live deployment и испытание реальной перезагрузкой.

## Проверка cleanup 7 октября 2026 года

После переноса fixture assertions в tests и удаления исторических CLI controls: 106 Python tests PASS, PowerShell parsing PASS, native install/collision/crash/upgrade/Resume/rollback PASS. Непустая старая legacy-конфигурация отказывается до filesystem/SQLite/API действий при обоих значениях recovery_enabled. Candidate и reachable Git history проверены на private paths/secrets. Полный ASR/LLM integration после этого cleanup повторно не запускался; его evidence выше относится к проверке 6 октября.

## Проверка повторного запуска Docker 7 октября 2026 года

113 Python tests PASS: timeout/OSError engine probe, присутствующий процесс, неизвестный/ошибочный process query, повторная проверка перед launch и запуск при подтверждённом отсутствии. Реальный запрос процессов Windows + искусственный таймаут engine на изолированном watchdog: 0 новых GUI spawns. Неизвестное состояние отражается как degraded health. Проверка физической перезагрузки и длительное наблюдение за интерфейсом Docker в эту проверку не входили.

## Фоновый ввод Watchdog — 7 октября 2026 года

125 Python tests PASS. Hidden PowerShell probes явно получают stdin=DEVNULL: фоновые задачи pythonw не требуют консольного ввода. Native read-only Inspect из собственной задачи с principal/settings рабочего Watchdog прошёл за 13,85 с: задачи trusted/Running, Bridge process_matches=true. Это отдельная проверка Inspect; полный scheduled watchdog pass, cold login и перезагрузка ею не подтверждаются.
