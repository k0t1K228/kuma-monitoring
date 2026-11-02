# Локальный отказоустойчивый контур мониторинга для Uptime Kuma

Стенд из двух независимых docker-compose слоёв, жизненным циклом которых управляет
Dagger-модуль (Python SDK). Dagger работает с Docker-демоном хоста через проброс
сокета `/var/run/docker.sock`.

| Слой | Compose-файл | Состав |
|------|--------------|--------|
| 1. Приложение и БД | `compose/app.yml` | Uptime Kuma 2.x, MariaDB 11.4 (одна нода, данные в именованном volume) |
| 2. Наблюдаемость | `compose/monitoring.yml` | Prometheus, Grafana, node-exporter, cAdvisor, mysqld-exporter, blackbox-exporter |

```
                        общая сеть Docker: kuma-shared
  ┌──────────────── слой 1 ─────────────────┐   ┌────────────────── слой 2 ──────────────────┐
  │  uptime-kuma :3001 ───────► mariadb     │   │  prometheus :9090 ─► grafana :3000         │
  └──────▲───────────────────────▲──────────┘   │     ▲  ▲  ▲  ▲                            │
         │ HTTP-проба            │ SQL-метрики  │     │  │  │  └ blackbox-exporter (HTTP)   │
         └───────────────────────┼──────────────┼─────┘  │  └─── mysqld-exporter            │
                                 └──────────────┼────────┘      cAdvisor (контейнеры)       │
                                                │               node-exporter (хост)        │
                                                └────────────────────────────────────────────┘
```

Что собирается и где смотреть:

- **Контейнеры** (CPU, RAM, сеть): cAdvisor.
- **Хост** (CPU, RAM, load): node-exporter.
- **MariaDB** (доступность, соединения, запросы/с, uptime): mysqld-exporter.
- **Uptime Kuma** (доступность и latency): HTTP-проба через blackbox-exporter.
  Собственный `/metrics` Kuma закрыт авторизацией, поэтому не используется.
- **Дашборд** «Kuma Contour Health» (`grafana/dashboards/kuma-health.json`): плашка
  «ОБЩИЙ СТАТУС» (ВСЁ ХОРОШО / ВСЁ ПЛОХО), статусы Kuma и MariaDB, ресурсы контейнеров и хоста.

## Требования

Проверено на: Arch Linux (cgroup v2), Docker 29.9.2 (по умолчанию с containerd image store),
Docker Compose 5.6.1, Dagger CLI v0.21.11. Go не нужен: модуль написан на Python,
окружение для него Dagger собирает сам внутри контейнера.

Нужны: Docker Engine, плагин Docker Compose, Dagger CLI, доступ в интернет (для скачивания образов),
свободные порты хоста **3000, 3001, 9090**.

### Версии образов

Все образы закреплены, кроме двух намеренно плавающих (LTS и мажорный тег):

| Компонент | Образ |
|-----------|-------|
| MariaDB | `mariadb:11.4` (LTS, патч-версии обновляются) |
| Uptime Kuma | `louislam/uptime-kuma:2` (мажорный тег; при проверке 2.5.9) |
| Prometheus | `prom/prometheus:v3.15.1` |
| Grafana | `grafana/grafana:13.3.0` |
| node-exporter | `prom/node-exporter:v1.12.1` |
| mysqld-exporter | `prom/mysqld-exporter:v0.21.0` |
| blackbox-exporter | `prom/blackbox-exporter:v0.29.1` |
| cAdvisor | `ghcr.io/google/cadvisor:0.60.3` |
| docker CLI для Dagger | `docker:cli`, закреплён по digest в `src/kuma_monitoring/main.py` |

### Подготовка окружения (Arch Linux)

```bash
sudo pacman -Syu
sudo pacman -S --needed docker docker-compose docker-buildx git jq curl
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # затем ПЕРЕЛОГИНЬТЕСЬ (закрытия терминала недостаточно)

mkdir -p ~/.local/bin
curl -fsSL https://dl.dagger.io/dagger/install.sh | BIN_DIR=$HOME/.local/bin sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
```

Проверка окружения (после перелогина):

```bash
id -nG | grep -w docker            # группа docker есть
docker run --rm hello-world        # Docker работает без sudo
docker compose version
dagger version
```

На других дистрибутивах Linux шаги те же, меняется только установка Docker. macOS и Windows (WSL)
не проверялись.

## Запуск

Все команды выполняются из корня репозитория. Первый запуск дольше остальных:
Dagger поднимает свой движок и скачивает образы. Каждая команда `dagger call` тратит
около 25 секунд на загрузку модуля.

### Одной командой

```bash
dagger call deploy-all --docker-sock=/var/run/docker.sock --grafana=tcp://localhost:3000
```

Команда последовательно выполняет `deploy-app`, `deploy-monitoring`, `configure-grafana`
и `validate`. В конце должно быть `ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ`. Запуск с чистого стенда при уже
скачанных образах занимает около 1,5 минут.

### По шагам (три обязательные команды)

```bash
# 1. MariaDB + Uptime Kuma, связывает их и ждёт healthy
dagger call deploy-app --docker-sock=/var/run/docker.sock

# 2. Prometheus, Grafana и exporters
dagger call deploy-monitoring --docker-sock=/var/run/docker.sock

# 3. Data Source и дашборд через Grafana API (идемпотентно: повторный запуск обновляет)
dagger call configure-grafana --grafana=tcp://localhost:3000
```

Порядок важен: `configure-grafana` требует уже запущенную Grafana.

Дополнительно:

```bash
dagger call validate --docker-sock=/var/run/docker.sock     # проверка здоровья платформы
dagger functions                                            # список всех функций модуля
```

Функции модуля: `deploy-app`, `deploy-monitoring`, `configure-grafana`, `deploy-all`, `validate`, `chaos`.

Сокет передаётся явным аргументом `--docker-sock`: у Dagger нет значения по умолчанию
для аргументов типа Socket, а прямой доступ к хосту из модуля запрещён. Для Grafana
хостовой порт пробрасывается в Dagger тем же образом (`--grafana=tcp://localhost:3000`).
Учётные данные Grafana при необходимости передаются флагами `--admin-user` и `--admin-password`.

### Первый вход в Uptime Kuma

Откройте http://localhost:3001 и создайте администратора (это делается один раз и сохраняется
в MariaDB). Мониторы самого Kuma нужны только для демонстрации: статус сервиса на дашборде
считается независимой HTTP-пробой.

## Где что доступно

| Сервис | Адрес | Учётные данные |
|--------|-------|----------------|
| Uptime Kuma | http://localhost:3001 | создаётся при первом входе |
| Grafana | http://localhost:3000 (дашборд: `/d/kuma-health`) | `admin` / `admin12345` |
| Prometheus | http://localhost:9090 | без авторизации |

Остальные компоненты (MariaDB, exporters) снаружи не публикуются и доступны только по сети `kuma-shared`.
Пароли MariaDB и exporter заданы значениями по умолчанию в `compose/app.yml`
(это учебный стенд, см. раздел «Ограничения»).

## Как проверить работоспособность

1. **Автоматически**: `dagger call validate --docker-sock=/var/run/docker.sock` проверяет, что
   запущены все 8 контейнеров, все таргеты Prometheus в состоянии `up`, «ОБЩИЙ СТАТУС» равен 1,
   а дашборд существует в Grafana.
2. **Контейнеры**: `docker ps --filter name=kuma-` — у MariaDB, Uptime Kuma и cAdvisor статус `healthy`.
3. **Prometheus**: http://localhost:9090/targets — все 6 таргетов в состоянии UP. Запрос
   `mysql_up` и `probe_success` должен давать `1`.
4. **Grafana**: http://localhost:3000/d/kuma-health — плашка «ОБЩИЙ СТАТУС» зелёная
   («ВСЁ ХОРОШО»), на панелях есть данные (графики появляются через ~1 минуту после старта).
5. **Отказ**: автоматически командой `chaos` (см. следующий раздел) либо вручную:
   `docker stop kuma-mariadb` — через 12–15 секунд плашка станет красной («ВСЁ ПЛОХО»);
   `docker start kuma-mariadb` — через 7–10 секунд снова зелёной.

## Chaos-тест

```bash
dagger call chaos --docker-sock=/var/run/docker.sock --target=kuma-mariadb
dagger call chaos --docker-sock=/var/run/docker.sock --target=kuma-uptime-kuma
```

Параметры: `--target` (`kuma-mariadb` или `kuma-uptime-kuma`, по умолчанию MariaDB) и
`--mode` (`stop`, `crash` или `both`, по умолчанию `both`). Перед тестом платформа должна быть
здорова (`validate`). Статус «ОБЩИЙ СТАТУС» читается через Grafana (прокси к Prometheus, тот же запрос,
что в плашке дашборда); если прокси недоступен, тест предупредит и прочитает его из Prometheus напрямую.

- **Фаза `stop`**: `docker stop` целевого контейнера. Тест проверяет, что дашборд перешёл в «ВСЁ ПЛОХО» (пункт а),
  затем возвращает контейнер командой `docker start` и проверяет возврат в «ВСЁ ХОРОШО».
  Возврат здесь ручной сознательно: Docker **не перезапускает вручную остановленные контейнеры** ни при
  `unless-stopped`, ни при `always`, поэтому по `docker stop` проверить restart-policy нельзя.
- **Фаза `crash`**: процесс контейнера убивается `SIGKILL` по PID хоста в обход Docker API
  (одноразовый контейнер с `--privileged --pid=host`). Для Docker это падение процесса, поэтому restart-policy
  (`unless-stopped`) перезапускает контейнер без вмешательства. Тест проверяет рост `RestartCount`, healthy
  и возврат статуса в «ВСЁ ХОРОШО» (пункт б). `docker kill` для этой цели непригоден: Docker считает его ручной остановкой.

Типичное время на проверенной машине: красный статус через 12–15 с после начала `docker stop`; после
`docker start` зелёный через 7–10 с; при падении контейнер перезапускается за 0–1 с и становится healthy
за 6–10 с. При падении окно «ВСЁ ПЛОХО» короче интервала сбора метрик (15 с), поэтому оно может не попасть
на дашборд (тест сообщит об этом строкой «наблюдение» и не считает это ошибкой); переход в красное
стабильно демонстрирует фаза `stop`. Если тест прерывается, целевой контейнер возвращается в работу автоматически.

## Остановка и очистка

```bash
docker compose -f compose/monitoring.yml down -v     # слой 2 (с данными Prometheus и Grafana)
docker compose -f compose/app.yml down               # слой 1, данные Kuma и MariaDB сохраняются
docker compose -f compose/app.yml down -v            # слой 1 вместе с данными
docker network rm kuma-shared                        # общая сеть
```

## Структура репозитория

```
dagger.json, pyproject.toml, src/kuma_monitoring/main.py   Dagger-модуль (Python SDK)
compose/app.yml                  слой 1: MariaDB + Uptime Kuma
compose/monitoring.yml           слой 2: Prometheus, Grafana, exporters
prometheus/                      Dockerfile и prometheus.yml (конфиг запекается в образ)
grafana/datasource.json          Data Source (накатывается через API)
grafana/dashboards/*.json        дашборды (накатываются через API)
README.md, AI_REVIEW.md
```

## Принятые решения

- **Конфиги запекаются в образы (`build:`), а не монтируются.** Dagger запускает `docker compose`
  из контейнера, а bind-mount относительных путей разрешает демон хоста и не найдёт файлы внутри
  контейнера Dagger. Контекст сборки клиент передаёт демону по API, поэтому такой вариант работает
  откуда угодно. Датасорс и дашборды вообще не монтируются: их накатывает `configure-grafana` через API.
- **Общая внешняя сеть `kuma-shared`.** Слои независимы, но Prometheus должен видеть Kuma и MariaDB.
  Сеть создаёт Dagger-функция (идемпотентно).
- **Кэш Dagger.** Он кэширует результат exec-шагов. В каждую функцию добавляется переменная с
  текущим временем, чтобы повторный вызов реально выполнялся.
- **Пользователь для mysqld-exporter** создаётся одноразовым сервисом `mariadb-exporter-user`
  с минимальными правами (`PROCESS`, `REPLICATION CLIENT`, `SELECT`). Он идемпотентен и работает и на существующем томе.
- **Uptime Kuma 2.x** (тег `louislam/uptime-kuma:2`): MariaDB поддерживается только с версии 2.0.
  Подключение настраивается переменными `UPTIME_KUMA_DB_*`. Не используйте тег `latest`:
  на Docker Hub он может указывать на 1.x, где эти переменные игнорируются и Kuma молча уходит на SQLite.
- **cAdvisor 0.60.3.** На Docker 29 с хранилищем containerd версия 0.56.1 падала с `nil pointer dereference`
  (исправлено в 0.56.2). Менять настройки Docker не требуется.
- **Проверка успеха.** Каждая функция ждёт реальной готовности (healthy / `/ready` / API) и завершается
  с ошибкой по таймауту, а не рапортует успех сразу после `up -d`.

## Возможные проблемы

| Симптом | Что делать |
|---------|-----------|
| `permission denied ... docker.sock` | Вы не в группе `docker` или не перелогинились: `sudo usermod -aG docker $USER`, затем перелогин. |
| `TLS handshake timeout` при скачивании образов (в `docker pull` или `dagger call`) | Нестабильная связь с Docker Hub (например, через VPN). Повторите команду: скачанное сохраняется в кэше. |
| `port is already allocated` | Порт 3000, 3001 или 9090 занят: `ss -ltnp \| grep -E ':3000\|:3001\|:9090'`. |
| `dagger: command not found` | Добавьте `~/.local/bin` в `PATH` (см. подготовку окружения). |
| Панели Grafana `No data` сразу после старта | Подождите 1–2 минуты: Prometheus собирает данные с нуля. |
| В метриках контейнеров только хеши вместо имён | Старая версия cAdvisor на Docker 29; используйте указанную в `compose/monitoring.yml`. |

## Ограничения

- Учётные данные (Grafana, MariaDB, exporter) заданы по умолчанию для локального стенда. Для реального
  использования их нужно выносить в секреты.
- Доступ к `docker.sock` эквивалентен root на хосте, а cAdvisor запускается с `privileged: true`.
  Это приемлемо только для локального стенда.
- Порты Grafana, Kuma и Prometheus опубликованы на всех интерфейсах хоста.
- MariaDB работает одной нодой (Galera не реализована).
- Фаза `crash` chaos-теста запускает одноразовый привилегированный контейнер (`--privileged --pid=host`),
  чтобы убить процесс снаружи; для локального стенда это допустимо.
