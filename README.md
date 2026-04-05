# YD2DBX

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`yd2dbx` — safe-first CLI на Python для первой волны миграции **Yandex Disk → Dropbox**: инвентарь и отчёты без скачивания содержимого файлов на диск, перенос документов с подтверждением и при необходимости server-side через `save_url`.

**Требования:** Python 3.11+. Внешние HTTP-зависимости не используются (только стандартная библиотека).

## Установка

```bash
git clone https://github.com/ComputerPers/YandexDisk_To_DropBox.git
cd YandexDisk_To_DropBox
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
yd2dbx --help
```

Без установки пакета можно вызывать обёртку из корня репозитория (она выставляет `PYTHONPATH=src`):

```bash
./yd2dbx --help
```

## Цели

- собрать инвентарь обоих облаков без скачивания содержимого файлов на ноутбук;
- построить `diff` до любых записей в Dropbox;
- переносить только документы, отсутствующие в Dropbox;
- вынести изображения, скриншоты, архивы, дистрибутивы, большие файлы и спорные случаи в отдельные отчёты;
- по возможности использовать server-side путь `Yandex download URL -> Dropbox save_url`.

## Важные ограничения

- Yandex Disk отдаёт `md5`, а Dropbox отдаёт `content_hash`; эти значения нельзя напрямую сравнивать как одинаковые хеши между облаками.
- `Dropbox save_url` не умеет отправлять кастомные заголовки к исходному URL. Значит, server-side перенос работает только если временный URL Яндекса доступен Dropbox как обычный URL.
- Если server-side перенос не удался, инструмент автоматически скачивает файл с Яндекса и загружает в Dropbox локально (fallback). Если и это не помогло, файл попадёт в `review_required`.

## Как получить токены

### Yandex Disk token

Нужен OAuth token для Disk API.

1. Откройте страницу регистрации приложения: [oauth.yandex.com](https://oauth.yandex.com/).
2. Войдите под тем аккаунтом Яндекса, чей Диск хотите читать.
3. Создайте приложение.
4. Дайте приложению права на Disk API. Для этой задачи минимально полезны права чтения и записи в Диск.
5. Скопируйте `Client ID` приложения.
6. Для ручного получения токена откройте URL такого вида:

```text
https://oauth.yandex.ru/authorize?response_type=token&client_id=<CLIENT_ID>
```

7. После подтверждения Яндекс вернёт access token, его и нужно использовать как `YANDEX_DISK_TOKEN`.

Официальные ссылки:
- [Yandex Disk API quickstart](https://yandex.com/dev/disk-api/doc/en/concepts/quickstart)
- [Yandex OAuth token guide](https://yandex.com/dev/id/doc/en/access)
- [Yandex app registration](https://yandex.com/dev/id/doc/en/register-client)

### Dropbox token

Нужен access token для Dropbox API.

Для этого проекта в Dropbox App Console действительно много опций. Ниже безопасный и практичный выбор именно для этого мигратора.

#### Что выбирать при создании app

1. Откройте [Dropbox App Console](https://www.dropbox.com/developers/apps).
2. Нажмите `Create app`.
3. Если Dropbox предлагает выбрать тип modern app со scopes, выбирайте scoped app.
4. На вопрос про уровень доступа к содержимому выбирайте `Full Dropbox`.
5. `App folder` не подходит, потому что мигратор должен читать уже существующие файлы и папки по всему вашему Dropbox, а не только внутри `/Apps/...`.

#### Какие permissions включить

Для текущего скрипта нужны операции:
- читать метаданные и список файлов в Dropbox;
- создавать папки;
- сохранять файлы в Dropbox через `save_url`.

Практически это означает:
- обязательно включить `files.metadata.read` для `list_folder`;
- включить scope на запись файлов в Dropbox, то есть `files.content.write`;
- если Dropbox App Console для write-операций предлагает дополнительные родственные file scopes, включайте те, что относятся именно к записи/созданию файлов и папок, но без лишних team/business permissions.

Идея такая:
- `files.metadata.read` нужно для сравнения структуры старого Dropbox;
- `files.content.write` нужно для `save_url` и создания целевой структуры;
- team/business/admin scopes этому проекту не нужны.

#### Какой токен брать

**Рекомендуемый способ — refresh token с автообновлением:**

1. Создайте app (scoped, Full Dropbox).
2. Включите permissions: `files.metadata.read`, `files.content.write`.
3. В App Settings найдите `App key` и `App secret`.
4. Запустите интерактивную настройку:

```bash
./yd2dbx setup-dropbox
```

5. Скрипт запросит App Key, App Secret, даст ссылку для авторизации в браузере, а затем попросит код.
6. По итогу сохранит refresh token в `.dropbox`.
7. Access token будет автоматически обновляться при истечении (каждые ~4 часа), без вашего участия.

**Альтернативный способ — ручной короткоживущий токен:**

- В разделе `OAuth 2` на странице app settings нажмите `Generate`.
- Положите полученный access token в `.dropbox`.
- Этот токен живёт ~4 часа, после чего нужно генерировать новый вручную.

#### Что именно рекомендую выбрать

Если хотите пройти мастер без сомнений, ориентируйтесь на такой набор:

- access type: `Full Dropbox`
- app type: scoped app
- permissions: минимум `files.metadata.read` и `files.content.write`
- token method: `./yd2dbx setup-dropbox` (refresh token, автообновление)

#### Чего не выбирать

- `App folder` — не подойдёт для сравнения с уже существующим Dropbox;
- Team / Business / Admin permissions — не нужны;
- лишние permissions “на всякий случай” тоже лучше не включать.

Официальные ссылки:
- [Dropbox getting started](https://www.dropbox.com/developers/reference/getting-started)
- [Dropbox OAuth guide](https://developers.dropbox.com/oauth-guide)
- [Generate an access token for your own account](https://dropbox.tech/developers/generate-an-access-token-for-your-own-account)

## Конфигурация

Переменные окружения:

- `YANDEX_DISK_TOKEN`: OAuth token Yandex Disk REST API.
- `DROPBOX_TOKEN`: access token Dropbox API.
- `YD2DBX_ROOT`: корневая папка для миграции, по умолчанию `/`.
- `YD2DBX_REPORT_DIR`: папка для отчётов, по умолчанию `reports`.
- `YD2DBX_DRY_RUN`: пока зарезервирована; фактическая запись в Dropbox всё равно требует явный `--execute`.
- `YD2DBX_LARGE_FILE_THRESHOLD_MB`: порог для large-file workflow, по умолчанию `256`.
- `YD2DBX_MAX_RETRIES`: число повторов server-side transfer, по умолчанию `3`.
- `YD2DBX_MAX_POLLS`: максимум опросов Dropbox job перед переводом файла в `review_required`, по умолчанию `300`.
- `YD2DBX_POLL_INTERVAL_SECONDS`: интервал опроса async job, по умолчанию `2`.
- `YD2DBX_LOG_LEVEL`: если задать `DEBUG`, `INFO` или `WARNING`, дублировать логи ещё и в stderr (по умолчанию подробный лог только в файл в каталоге `logs/`).

## Запускалка в корне

В корне проекта есть скрипт `./yd2dbx`.

Он:
- автоматически запускает CLI из корня репозитория;
- сам выставляет `PYTHONPATH=src`;
- в первую очередь использует токены из `./.yadisk` и `./.dropbox`;
- переменные окружения для токенов используются только как fallback, если файлов нет.

Основной сценарий:

```bash
./yd2dbx run
```

## Полный автоматический запуск

### 1. Перейти в каталог проекта

```bash
cd /путь/к/YandexDisk_To_DropBox
```

### 2. Подготовить токены

**Yandex Disk** — положите токен в файл:

```bash
printf '%s\n' 'ваш_yandex_token' > .yadisk
```

**Dropbox** — рекомендуемый способ с автообновлением:

```bash
./yd2dbx setup-dropbox
```

Скрипт интерактивно запросит App Key, App Secret и код авторизации, после чего сохранит refresh token в `.dropbox`. Access token будет обновляться автоматически каждые ~4 часа.

**Альтернатива** — вручную положить короткоживущий access token:

```bash
printf '%s\n' 'ваш_dropbox_access_token' > .dropbox
```

Если файлов `.yadisk` и `.dropbox` нет, можно использовать запасной вариант через переменные окружения:

```bash
export YANDEX_DISK_TOKEN="ваш_yandex_token"
export DROPBOX_TOKEN="ваш_dropbox_token"
```

Что важно:
- основной рекомендуемый источник токенов — файлы `./.yadisk` и `./.dropbox`;
- `YD2DBX_ROOT` ограничивает область миграции; если не задавать, используется весь диск (`/`);
- `YD2DBX_REPORT_DIR` задаёт каталог итоговых отчётов;
- состояние миграции хранится отдельно в SQLite-файле `.yd2dbx.db`.

### 3. Запустить миграцию одной командой

```bash
./yd2dbx run
```

Что происходит:
- выполняются preflight-проверки доступа к Yandex Disk и Dropbox;
- инвентарь Yandex Disk снимается постранично и сохраняется в SQLite;
- инвентарь Dropbox снимается постранично и сохраняется в ту же SQLite-базу;
- файлы сразу классифицируются: документы идут в primary sync, скриншоты и dev-junk пропускаются, изображения/архивы/.git уходят в отдельные треки;
- строится `diff` без загрузки всех записей в память;
- перед реальной записью в Dropbox показывается сводка и запрашивается подтверждение;
- после подтверждения запускается server-side sync через `Yandex download URL -> Dropbox save_url`;
- после каждого файла результат пишется в SQLite, поэтому процесс можно продолжать после сбоя.

Примеры прогресса в терминале:

```text
[Yandex] 47000 files collected
[Dropbox] 32000 files collected
[Classify] 54000 / 54000
[Sync] 42 / 150 /docs/report.pdf
```

### 4. Если процесс прервался

Просто запустите ту же команду снова:

```bash
./yd2dbx run
```

Что происходит:
- повторная инвентаризация не начинается с нуля;
- берётся состояние из `.yd2dbx.db`;
- если inventory уже завершён, команда продолжит со стадии подтверждения или sync;
- уже перенесённые файлы повторно не отправляются.

### 5. Если нужно начать заново

```bash
./yd2dbx run --reset
```

Или явно указать другой файл состояния:

```bash
./yd2dbx run --db custom-state.db
```

### 6. Где смотреть результат

Итоговые отчёты сохраняются в `reports/`:
- `reports/run.json`
- `reports/run.md`
- `reports/run.csv`

SQLite-состояние по умолчанию:
- `.yd2dbx.db`

### 7. Что именно НЕ скачивается на ноутбук

Во время inventory скрипт не читает содержимое PDF, DOCX, JPG и других файлов. Он запрашивает только метаданные:
- путь;
- размер;
- modified time;
- mime type;
- хеш источника, если API его отдаёт.

Байты файла начинают передаваться только на фазе `save_url`, и тогда Dropbox тянет файл напрямую по временному URL Яндекса. Ноутбук в потоке данных не участвует.

## Команды

Настройка Dropbox (refresh token):

```bash
./yd2dbx setup-dropbox
```

Основная команда:

```bash
./yd2dbx run
```

Продолжить после сбоя:

```bash
./yd2dbx run
```

Начать заново:

```bash
./yd2dbx run --reset
```

Указать альтернативную SQLite-базу состояния:

```bash
./yd2dbx run --db custom-state.db
```

Расширенный ручной режим, если нужен полный контроль.

Снять инвентарь:

```bash
./yd2dbx inventory
```

Построить diff:

```bash
./yd2dbx diff
```

Сухой прогон синка:

```bash
./yd2dbx sync
```

Боевой server-side sync:

```bash
./yd2dbx sync --execute
```

Использовать сохранённый snapshot инвентаря:

```bash
./yd2dbx diff --inventory-json reports/inventory.json
./yd2dbx sync --inventory-json reports/inventory.json
```

Отрисовать Markdown из JSON-отчёта:

```bash
./yd2dbx report reports/sync.json
```

## Категории отчётов

- `missing_in_dropbox`: кандидаты на перенос в первой волне.
- `exact_metadata_match_candidate`: вероятные совпадения по пути, размеру и времени.
- `path_exists_but_differs`: путь уже есть в Dropbox, но метаданные отличаются.
- `unsupported_for_first_pass`: изображения, архивы, большие файлы и другие отложенные категории.
- `explicit_skip`: файлы, намеренно исключённые из первой волны, например скриншоты.

## Безопасность и приватность

- Не коммитьте токены и секреты. Файлы `.yadisk`, `.dropbox`, база `.yd2dbx.db`, каталоги `logs/` и `reports/` перечислены в `.gitignore`.
- Репозиторий не должен содержать персональные списки исключений: используйте `sync_exclude.txt` как шаблон и правьте локально (или держите копию вне git).
- Утилита обращается к API Yandex Disk и Dropbox от вашего имени; ознакомьтесь с политиками обоих сервисов.

## Разработка и тесты

```bash
cd /путь/к/YandexDisk_To_DropBox
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

После `pip install -e .` достаточно `python3 -m unittest discover -s tests -t .` из корня проекта.

## Лицензия

Проект распространяется по лицензии MIT, см. файл [LICENSE](LICENSE).
