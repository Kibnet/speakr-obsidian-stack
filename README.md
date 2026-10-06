# Speakr → Obsidian: локальный стек для Windows

Основа для собственной системы: Speakr хранит и редактирует расшифровки, WhisperX распознаёт речь и разделяет говорящих, Ollama готовит резюме, bridge публикует неизменяемые Markdown-заметки в Obsidian. Watchdog восстанавливает остановленные компоненты после входа в Windows и периодически проверяет состояние. Очередь хранится на диске.

Это самостоятельный deployment-репозиторий. Speakr собирается из закреплённого публичного коммита; его исходники не включены в Git этого проекта. Личные записи, настройки и токены автора здесь отсутствуют.

## Что подготовить

- Windows 10/11, PowerShell 5.1, Docker Desktop с WSL2 и Linux containers. Docker должен запускаться под вашим пользователем.
- NVIDIA GPU, поддерживаемый драйвер и GPU passthrough в Docker. Профиль `blackwell-5070ti` повторяет настройки RTX 5070 Ti; `nvidia` отключает специальные настройки Ollama и уменьшает ASR batch. Проверьте совместимость образа ASR с вашей GPU — универсальная поддержка не обещается.
- Python с `python.exe` и `pythonw.exe`, ffmpeg/ffprobe в PATH, Git и установленный Ollama. Проверенные версии и границы проверок: [docs/validation.md](docs/validation.md).
- Свой Hugging Face token и доступ к моделям diarization. [WhisperX setup](https://github.com/murtaza-nasir/whisperx-asr-service) описывает доступ к моделям; условия принимает владелец аккаунта.
- Папку Obsidian vault. Python bridge использует только стандартную библиотеку.

Первый build/pull скачивает исходники, образы и модели. Обработка записей работает локально: app блокирует внешние HTTP destinations и proxy environment. Настройка облачных провайдеров в этом варианте не поддерживается. Это ограничение также блокирует внешние интеграции Speakr.

## Первая установка

Команды выполняются из корня клонированного репозитория в PowerShell. Выберите собственное имя `project` и свободные порты, особенно если похожий стек уже запущен. Используйте отдельные внешние каталоги для runtime, моделей, vault и аудио. Runtime не должен пересекаться с checkout, vault, sources или входными файлами настроек.

1. Скопируйте `config/stack.example.json` и `config/secrets.example.env` **вне репозитория и runtime**, например в `C:\LocalConfig\my-speakr`. Создайте каталоги для vault и sources. Укажите свои пути к executable, `project`, порты, выбранный hardware profile и папки. `sources: []` допустим: сначала можно использовать ручное добавление. Секретный файл содержит `ADMIN_EMAIL`, пароль от 16 символов и `HF_TOKEN`; примеры не принимаются. Пароль может содержать `$`, но однокавычки в env inputs не поддерживаются.

2. Выполните read-only preflight, затем подготовку собственного runtime:

```powershell
python scripts/stack.py preflight --config C:\LocalConfig\my-speakr\stack.json --secrets C:\LocalConfig\my-speakr\secrets.env
python scripts/stack.py prepare --config C:\LocalConfig\my-speakr\stack.json --secrets C:\LocalConfig\my-speakr\secrets.env
```

`prepare` требует пустой runtime. Создаёт `stack/` с собственными Compose/env, `automation/config.json` и manifest ownership. Генерирует `SECRET_KEY`. Пароли не выводятся. Не повторяйте Prepare поверх существующей установки.

3. Соберите Speakr. `--cold` отключает layer cache; без него используются проверенные build inputs и Docker cache:

```powershell
python scripts/stack.py build --config C:\LocalConfig\my-speakr\stack.json --cold
```

Сборка использует public source SHA и официальный FFmpeg source archive с SHA-256. На CPU это занимает время. Системные пакеты Debian и сети download должны быть доступны; это воспроизводимый рецепт, а не обещание побитово одинакового образа.

4. Запустите отдельный Ollama на настроенном порту. Для первой подготовки используйте отдельное окно PowerShell; не запускайте второй сервер на занятом порту:

```powershell
powershell.exe -NoProfile -File automation/start-ollama.ps1 -Root C:\LocalApps\my-speakr\automation
```

Пути должны соответствовать вашему `target_root`. При `managed_ollama: false` сервер запускается вашим способом, а репозиторий не управляет его Windows task. В другом окне создайте модель:

```powershell
python scripts/stack.py model --config C:\LocalConfig\my-speakr\stack.json
```

Скачивается базовая `qwen3.5:9b`, проверяется public digest из `config/dependencies.json`, создаётся alias с параметрами `models/Modelfile`. Чужой существующий alias не перезаписывается. Если tag изменился и digest не совпал, подготовка прекращается: [порядок обновления](docs/operations.md).

5. Запустите контейнеры, дождитесь загрузки ASR-моделей и проверьте страницу Speakr/health ASR:

```powershell
python scripts/stack.py up --config C:\LocalConfig\my-speakr\stack.json
python scripts/stack.py status --config C:\LocalConfig\my-speakr\stack.json
```

`up` создаёт только новые собственные контейнеры. Проверяет конфигурацию до запуска, затем image/container identities и доступ к Ollama **из app container**. При ошибке не открывает Ollama в LAN. Устраните причину и используйте `start` для уже созданных собственных контейнеров; не повторяйте `up` для их принятия.

6. Войдите в Speakr с вашими admin email/password. После готовности сервисов установите bridge/tasks:

```powershell
python scripts/stack.py install --config C:\LocalConfig\my-speakr\stack.json
```

Первая установка отмечает существующие файлы в sources как baseline: старый архив автоматически не загружается. Создаёт задачи под текущим пользователем: Bridge, Watchdog, при необходимости Ollama. Оставляет **maintenance pause**. Чужие задачи с совпавшими именами блокируют установку.

Завершите ручной Ollama из шага 4, если дальше им будет управлять собственная задача. Активируйте обработку:

```powershell
python scripts/stack.py resume --config C:\LocalConfig\my-speakr\stack.json
python scripts/stack.py status --config C:\LocalConfig\my-speakr\stack.json
```

Проверьте **свою** новую короткую запись: появление в Speakr, текст и резюме в заметке. Затем проверьте вход в Windows после реальной перезагрузки. Задачи требуют интерактивного входа пользователя; до входа Windows service не работает. Точные границы выполненной проверки приведены в [validation](docs/validation.md).

## Обычная работа

Новые стабильные аудиофайлы из `sources` попадают в очередь. Старые файлы или выбранные записи можно явно добавить:

```powershell
python C:\LocalApps\my-speakr\automation\enqueue.pyw C:\Audio\meeting.m4a
python C:\LocalApps\my-speakr\automation\bridge.py --status
```

`enqueue.pyw` открывает локальное подтверждение результата сохранения запроса. Для Explorer можно создать ярлык «Отправить» с `pythonw.exe` и этим скриптом; автоматическое изменение Explorer не выполняется.

Текст сохраняется и при известной ошибке резюме. Повторное резюме допускается только после проверки идентичности записи, событий и очереди. Существующие версии заметок не перезаписываются. Неизвестное состояние требует ручной проверки.

Пауза, backup, обновление, откат и ручное восстановление: [docs/operations.md](docs/operations.md). Тесты не используют личную установку:

```powershell
python -m unittest discover -s tests -v
powershell.exe -NoProfile -File tests/validate-candidate.ps1
powershell.exe -NoProfile -File tests/validate-docker-startup.ps1
python tests/integration.py
python tests/combined.py
```

Native tests создают уникальные временные задачи/каталоги; integration использует настоящий собранный Speakr, синтетические ASR/LLM и собственные контейнеры без GPU. Integration и combined запускайте после build. Combined проходит от пустого runtime через прерванный Up, восстановление новым процессом и обычный native Install до заметки, а также проверяет повторный Start после установки. Полное тестирование и ограничения: [docs/validation.md](docs/validation.md).

## Публикация своей копии

Перед публикацией проверьте `git status`, tracked files и отсутствие secrets/runtime. Создайте пустой репозиторий в своём GitHub аккаунте и выполните самостоятельно:

```powershell
git remote add origin https://github.com/YOUR_ACCOUNT/speakr-obsidian-stack.git
git push -u origin feat/local-transcription-stack
```

Не добавляйте `.work`, `.validation`, runtime, аудио, vault или токены. Лицензия — AGPL-3.0; источники и компоненты: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
