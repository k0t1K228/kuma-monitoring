"""Dagger-модуль: локальный отказоустойчивый контур мониторинга Uptime Kuma.

Dagger управляет хостовым Docker-демоном через проброс сокета /var/run/docker.sock:
внутри функций запускается контейнер docker:cli, который выполняет docker compose
против демона хоста.
"""
import time
from typing import Annotated

import dagger
from dagger import DefaultPath, Doc, Ignore, dag, function, object_type

# Образ с docker CLI + compose-плагином (при необходимости закрепляется по digest)
DOCKER_IMAGE = "docker@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb"

# Запрос для плашки «ОБЩИЙ СТАТУС» (то же выражение, что в Grafana-дашборде)
OVERALL_EXPR = (
    '(min(mysql_up) or vector(0)) * '
    '(min(probe_success{job="blackbox-http",instance="http://kuma-uptime-kuma:3001"}) or vector(0))'
)

SourceDir = Annotated[
    dagger.Directory,
    DefaultPath("/"),
    Ignore([".git", "sdk", ".venv", "**/__pycache__", "uv.lock"]),
    Doc("Корень репозитория (по умолчанию подставляется автоматически)"),
]
DockerSock = Annotated[
    dagger.Socket,
    Doc("Unix-сокет Docker-демона хоста, например /var/run/docker.sock"),
]
GrafanaSvc = Annotated[
    dagger.Service,
    Doc("Grafana на хосте, например tcp://localhost:3000"),
]
AdminUser = Annotated[str, Doc("Логин администратора Grafana")]
AdminPassword = Annotated[str, Doc("Пароль администратора Grafana")]

# ---------------------------------------------------------------- shell-скрипты

# Общая шапка: compose-плагин, общая сеть двух слоёв, ожидание healthy
PRELUDE = r"""
set -eu
docker compose version >/dev/null 2>&1 || apk add --no-cache docker-cli-compose >/dev/null
docker network inspect kuma-shared >/dev/null 2>&1 || docker network create kuma-shared >/dev/null

wait_healthy() {
  name="$1"; timeout="$2"; i=0
  while [ "$i" -lt "$timeout" ]; do
    s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)
    if [ "$s" = "healthy" ]; then echo "OK: $name healthy (${i}s)"; return 0; fi
    i=$((i+2)); sleep 2
  done
  echo "FAIL: $name не стал healthy за ${timeout}s (статус: $s)" >&2
  docker logs --tail 30 "$name" >&2 || true
  return 1
}
"""

DEPLOY_APP_SCRIPT = PRELUDE + r"""
docker compose -f compose/app.yml up -d
wait_healthy kuma-mariadb 120
wait_healthy kuma-uptime-kuma 180
docker compose -f compose/app.yml ps
"""

DEPLOY_MONITORING_SCRIPT = PRELUDE + r"""
wait_exec() {
  name="$1"; timeout="$2"; shift 2; i=0
  while [ "$i" -lt "$timeout" ]; do
    if docker exec "$name" "$@" >/dev/null 2>&1; then echo "OK: $name ready (${i}s)"; return 0; fi
    i=$((i+2)); sleep 2
  done
  echo "FAIL: $name не готов за ${timeout}s" >&2
  docker logs --tail 30 "$name" >&2 || true
  return 1
}

docker compose -f compose/monitoring.yml up -d --build
wait_exec kuma-prometheus 90 wget -qO- http://localhost:9090/-/ready
wait_exec kuma-grafana 90 wget -qO- http://localhost:3000/api/health
docker compose -f compose/monitoring.yml ps
"""

CONFIGURE_GRAFANA_SCRIPT = r"""
set -eu
apk add --no-cache curl jq >/dev/null
G=http://grafana:3000
CURL="curl -sS --fail-with-body -u $GF_USER:$GF_PASS -H Content-Type:application/json"

i=0
until curl -sf "$G/api/health" | jq -e '.database=="ok"' >/dev/null 2>&1; do
  i=$((i+2))
  if [ "$i" -ge 90 ]; then echo "FAIL: Grafana не отвечает" >&2; exit 1; fi
  sleep 2
done
echo "OK: Grafana доступна"

# Data source: создать или обновить по uid (команда идемпотентна)
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$GF_USER:$GF_PASS" "$G/api/datasources/uid/prometheus")
if [ "$code" = "200" ]; then
  M=PUT; U="$G/api/datasources/uid/prometheus"
else
  M=POST; U="$G/api/datasources"
fi
resp=$($CURL -X "$M" "$U" -d @grafana/datasource.json)
echo "$resp" | jq -c "{datasource: \"$M\", message}"
$CURL "$G/api/datasources/uid/prometheus/health" | jq -e '.status=="OK"' >/dev/null
echo "OK: datasource Prometheus health OK"

# Дашборды: статус каждого импорта проверяется явно
for f in grafana/dashboards/*.json; do
  resp=$(jq '{dashboard: ., overwrite: true}' "$f" | $CURL -X POST "$G/api/dashboards/db" -d @-)
  echo "$resp" | jq -e '.status=="success"' >/dev/null
  echo "$resp" | jq -c '{dashboard: .slug, status, url}'
done
"""

VALIDATE_SCRIPT = r"""
set -eu
apk add --no-cache jq >/dev/null

retry() {
  desc="$1"; timeout="$2"; shift 2; i=0
  while [ "$i" -lt "$timeout" ]; do
    if "$@" >/dev/null 2>&1; then echo "OK: $desc (${i}s)"; return 0; fi
    i=$((i+3)); sleep 3
  done
  echo "FAIL: $desc (за ${timeout}s не выполнилось)" >&2
  return 1
}

promget() { docker exec kuma-prometheus wget -qO- "http://localhost:9090$1"; }
promq() { q=$(jq -rn --arg x "$1" '$x|@uri'); promget "/api/v1/query?query=$q"; }

check_containers() {
  for c in kuma-mariadb kuma-uptime-kuma kuma-prometheus kuma-grafana \
           kuma-node-exporter kuma-cadvisor kuma-mysqld-exporter kuma-blackbox-exporter; do
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || { echo "не запущен: $c" >&2; return 1; }
  done
}
check_targets() {
  promget /api/v1/targets | jq -e '(.data.activeTargets|length)>0 and ([.data.activeTargets[]|select(.health!="up")]|length)==0'
}
check_overall() {
  promq "$OVERALL" | jq -e '.data.result[0].value[1]=="1"'
}
check_dashboard() {
  B=$(printf '%s:%s' "$GF_USER" "$GF_PASS" | base64 | tr -d '\n')
  docker exec kuma-grafana wget -qO- --header "Authorization: Basic $B" http://localhost:3000/api/dashboards/uid/kuma-health | jq -e '.dashboard.uid=="kuma-health"'
}

retry "все 8 контейнеров запущены" 30 check_containers
retry "все таргеты Prometheus в состоянии up" 120 check_targets
retry "ОБЩИЙ СТАТУС = 1 (MariaDB + Uptime Kuma)" 120 check_overall
retry "дашборд kuma-health есть в Grafana" 30 check_dashboard
echo "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ"
"""


CHAOS_SCRIPT = r"""
apk add --no-cache jq >/dev/null

fail() { echo "FAIL: $*" >&2; exit 1; }

case "$TARGET" in kuma-mariadb|kuma-uptime-kuma) ;; *) fail "target: kuma-mariadb или kuma-uptime-kuma" ;; esac
case "$MODE" in stop|crash|both) ;; *) fail "mode: stop, crash или both" ;; esac

cleanup() {
  if [ "$(docker inspect -f '{{.State.Running}}' "$TARGET" 2>/dev/null)" != "true" ]; then
    echo "очистка: возвращаю $TARGET в работу" >&2
    docker start "$TARGET" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

now() { date +%s; }
promget() { docker exec kuma-prometheus wget -qO- "http://localhost:9090$1"; }
grafget() {
  B=$(printf '%s:%s' "$GF_USER" "$GF_PASS" | base64 | tr -d '\n')
  docker exec kuma-grafana wget -qO- --header "Authorization: Basic $B" "http://localhost:3000$1"
}

Q=$(jq -rn --arg x "$OVERALL" '$x|@uri')
GQ="/api/datasources/proxy/uid/prometheus/api/v1/query?query=$Q"
# Статус читаем через Grafana (путь, которым питается дашборд); если не вышло, напрямую из Prometheus
if grafget "$GQ" 2>/dev/null | jq -e '.data.result[0].value[1]' >/dev/null 2>&1; then
  SRC=grafana
else
  SRC=prometheus
  echo "WARN: через Grafana статус прочитать не удалось, читаю из Prometheus"
fi

status() {
  if [ "$SRC" = grafana ]; then r=$(grafget "$GQ" 2>/dev/null || true); else r=$(promget "/api/v1/query?query=$Q" 2>/dev/null || true); fi
  v=$(printf '%s' "$r" | jq -r '.data.result[0].value[1] // "none"' 2>/dev/null || true)
  printf '%s' "${v:-none}"
}

rm -f /tmp/seen0
# wait_status <значение> <таймаут>: печатает, сколько секунд ждали
wait_status() {
  want="$1"; timeout="$2"; t0=$(now)
  while :; do
    s=$(status)
    [ "$s" = "0" ] && : > /tmp/seen0
    if [ "$s" = "$want" ]; then echo $(( $(now) - t0 )); return 0; fi
    [ $(( $(now) - t0 )) -ge "$timeout" ] && return 1
    sleep 2
  done
}

phase_stop() {
  echo "--- фаза STOP: docker stop $TARGET"
  wait_status 1 120 >/dev/null || fail "перед тестом статус не ВСЁ ХОРОШО"
  echo "OK: исходный статус ВСЁ ХОРОШО (источник: $SRC)"
  ts=$(now)
  docker stop "$TARGET" >/dev/null
  echo "контейнер $TARGET остановлен (сама команда docker stop заняла $(( $(now) - ts )) с)"
  wait_status 0 90 >/dev/null || fail "(а) статус ВСЁ ПЛОХО не появился за 90 с"
  echo "OK (а): дашборд показывает ВСЁ ПЛОХО через $(( $(now) - ts )) с после начала docker stop"
  # Docker не перезапускает вручную остановленные контейнеры, поэтому поднимаем вручную
  docker start "$TARGET" >/dev/null
  wait_healthy "$TARGET" 180
  d=$(wait_status 1 240) || fail "после docker start статус не вернулся в ВСЁ ХОРОШО за 240 с"
  echo "OK: после docker start дашборд вернулся в ВСЁ ХОРОШО через ${d} с"
}

phase_crash() {
  echo "--- фаза CRASH: падение процесса $TARGET, восстановление только по restart-policy"
  wait_status 1 120 >/dev/null || fail "перед тестом статус не ВСЁ ХОРОШО"
  pol=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$TARGET")
  [ "$pol" != "no" ] || fail "у $TARGET не задана restart-policy"
  echo "restart-policy: $pol"
  rc0=$(docker inspect -f '{{.RestartCount}}' "$TARGET")
  rm -f /tmp/seen0
  pid=$(docker inspect -f '{{.State.Pid}}' "$TARGET")
  img=$(docker inspect -f '{{.Config.Image}}' "$TARGET")
  # SIGKILL по PID хоста в обход Docker API. Для Docker это падение процесса, а не ручная
  # остановка (docker kill/stop отключают restart-policy), поэтому политика должна сработать.
  docker run --rm --privileged --pid=host --entrypoint sh "$img" -c "kill -9 $pid" >/dev/null
  echo "процесс $TARGET (PID хоста $pid) убит SIGKILL снаружи, дальше вмешательств нет"
  t0=$(now)
  while :; do
    rc=$(docker inspect -f '{{.RestartCount}}' "$TARGET")
    run=$(docker inspect -f '{{.State.Running}}' "$TARGET")
    [ "$(status)" = "0" ] && : > /tmp/seen0
    if [ "$rc" -gt "$rc0" ] && [ "$run" = "true" ]; then break; fi
    [ $(( $(now) - t0 )) -ge 60 ] && fail "(б) restart-policy не перезапустила $TARGET за 60 с"
    sleep 1
  done
  echo "OK (б): $TARGET перезапущен по restart-policy за $(( $(now) - t0 )) с (RestartCount $rc0 -> $rc)"
  wait_healthy "$TARGET" 180
  d=$(wait_status 1 240) || fail "после перезапуска статус не вернулся в ВСЁ ХОРОШО за 240 с"
  echo "OK (б): дашборд вернулся в ВСЁ ХОРОШО через ${d} с после выхода контейнера из healthy-ожидания"
  if [ -f /tmp/seen0 ]; then echo "наблюдение: окно ВСЁ ПЛОХО при падении было видно на дашборде"
  else echo "наблюдение: окно ВСЁ ПЛОХО короче интервала сбора метрик (15 с) и не попало в выборку"; fi
}

case "$MODE" in
  stop) phase_stop ;;
  crash) phase_crash ;;
  both) phase_stop; phase_crash ;;
esac
echo "CHAOS-ТЕСТ ПРОЙДЕН"
"""

# ------------------------------------------------------------------- помощники


def docker_cli(source: dagger.Directory, sock: dagger.Socket) -> dagger.Container:
    """Контейнер с docker CLI, сокетом хоста и файлами проекта."""
    return (
        dag.container()
        .from_(DOCKER_IMAGE)
        .with_unix_socket("/var/run/docker.sock", sock)
        .with_directory("/work", source)
        .with_workdir("/work")
        # Dagger кэширует exec: без этого повторный вызов ничего бы не выполнил
        .with_env_variable("CACHE_BUSTER", str(time.time()))
    )


async def _run(source: dagger.Directory, sock: dagger.Socket, script: str) -> str:
    return await docker_cli(source, sock).with_exec(["sh", "-c", script]).stdout()


async def _deploy_app(sock: dagger.Socket, source: dagger.Directory) -> str:
    return await _run(source, sock, DEPLOY_APP_SCRIPT)


async def _deploy_monitoring(sock: dagger.Socket, source: dagger.Directory) -> str:
    return await _run(source, sock, DEPLOY_MONITORING_SCRIPT)


async def _configure_grafana(
    grafana: dagger.Service, source: dagger.Directory, user: str, password: str
) -> str:
    return await (
        dag.container()
        .from_(DOCKER_IMAGE)
        .with_service_binding("grafana", grafana)
        .with_directory("/work", source)
        .with_workdir("/work")
        .with_env_variable("GF_USER", user)
        .with_env_variable("GF_PASS", password)
        .with_env_variable("CACHE_BUSTER", str(time.time()))
        .with_exec(["sh", "-c", CONFIGURE_GRAFANA_SCRIPT])
        .stdout()
    )


async def _validate(
    sock: dagger.Socket, source: dagger.Directory, user: str, password: str
) -> str:
    return await (
        docker_cli(source, sock)
        .with_env_variable("GF_USER", user)
        .with_env_variable("GF_PASS", password)
        .with_env_variable("OVERALL", OVERALL_EXPR)
        .with_exec(["sh", "-c", VALIDATE_SCRIPT])
        .stdout()
    )


async def _chaos(
    sock: dagger.Socket, source: dagger.Directory, user: str, password: str, target: str, mode: str
) -> str:
    return await (
        docker_cli(source, sock)
        .with_env_variable("GF_USER", user)
        .with_env_variable("GF_PASS", password)
        .with_env_variable("OVERALL", OVERALL_EXPR)
        .with_env_variable("TARGET", target)
        .with_env_variable("MODE", mode)
        .with_exec(["sh", "-c", PRELUDE + CHAOS_SCRIPT])
        .stdout()
    )



# ----------------------------------------------------------------- Dagger-API


@object_type
class KumaMonitoring:
    @function
    async def deploy_app(self, docker_sock: DockerSock, source: SourceDir) -> str:
        """Поднимает MariaDB и Uptime Kuma (слой 1) и ждёт их готовности."""
        return await _deploy_app(docker_sock, source)

    @function
    async def deploy_monitoring(self, docker_sock: DockerSock, source: SourceDir) -> str:
        """Поднимает Prometheus, Grafana и exporters (слой 2) и ждёт их готовности."""
        return await _deploy_monitoring(docker_sock, source)

    @function
    async def configure_grafana(
        self,
        grafana: GrafanaSvc,
        source: SourceDir,
        admin_user: AdminUser = "admin",
        admin_password: AdminPassword = "admin12345",
    ) -> str:
        """Через Grafana API накатывает Data Source и дашборды (идемпотентно)."""
        return await _configure_grafana(grafana, source, admin_user, admin_password)

    @function
    async def validate(
        self,
        docker_sock: DockerSock,
        source: SourceDir,
        admin_user: AdminUser = "admin",
        admin_password: AdminPassword = "admin12345",
    ) -> str:
        """Проверяет здоровье всей платформы (контейнеры, таргеты, общий статус, дашборд)."""
        return await _validate(docker_sock, source, admin_user, admin_password)

    @function
    async def deploy_all(
        self,
        docker_sock: DockerSock,
        grafana: GrafanaSvc,
        source: SourceDir,
        admin_user: AdminUser = "admin",
        admin_password: AdminPassword = "admin12345",
    ) -> str:
        """Поднимает платформу целиком, настраивает Grafana и валидирует статус всех систем."""
        parts = [
            "=== deploy-app ===",
            await _deploy_app(docker_sock, source),
            "=== deploy-monitoring ===",
            await _deploy_monitoring(docker_sock, source),
            "=== configure-grafana ===",
            await _configure_grafana(grafana, source, admin_user, admin_password),
            "=== validate ===",
            await _validate(docker_sock, source, admin_user, admin_password),
        ]
        return "\n".join(parts)

    @function
    async def chaos(
        self,
        docker_sock: DockerSock,
        source: SourceDir,
        target: Annotated[str, Doc("Контейнер для отказа: kuma-mariadb или kuma-uptime-kuma")] = "kuma-mariadb",
        mode: Annotated[
            str,
            Doc("stop: docker stop (проверка 'Все Плохо'); crash: падение процесса SIGKILL (проверка restart-policy); both: оба"),
        ] = "both",
        admin_user: AdminUser = "admin",
        admin_password: AdminPassword = "admin12345",
    ) -> str:
        """Chaos-тест: отказ контейнера, переход дашборда в 'Все Плохо' и восстановление."""
        return await _chaos(docker_sock, source, admin_user, admin_password, target, mode)
