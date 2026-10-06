# Проверки и их границы

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

Подробные logs, temporary roots и fixture transcripts сохраняются локально в ignored `.validation/` и временных каталогах; в Git входят тесты и этот итог. Публикация на GitHub, live deployment и испытание перезагрузкой не выполнялись.
