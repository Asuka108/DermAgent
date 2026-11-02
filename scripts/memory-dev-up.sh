#!/bin/sh
# 本地记忆层启动/自检：Redis → MySQL（建库建表）→ Milvus（可选）。
# 任一项未就绪不视为失败——记忆层按计划 §10 降级，对话流程不受影响。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SERVER="$ROOT/web/backend/server"
PY="${PYTHON:-python3}"

echo "== 1/3 Redis =="
if command -v redis-cli >/dev/null 2>&1; then
  if redis-cli -u "${DERM_REDIS_URL:-redis://127.0.0.1:6379/0}" ping >/dev/null 2>&1; then
    echo "ok: Redis 可用"
  else
    echo "warn: Redis 未响应；短期窗口将回退 sessions.json（degraded.short_term=true）"
    echo "      macOS: brew services start redis"
  fi
else
  echo "warn: 未找到 redis-cli；跳过检查（服务端仍会按需连接）"
fi

echo "== 2/3 MySQL =="
if command -v mysqladmin >/dev/null 2>&1 && mysqladmin ping --silent >/dev/null 2>&1; then
  echo "ok: MySQL 已在运行"
else
  MYSQL_SERVER="${MYSQL_SERVER:-$HOME/mysql/support-files/mysql.server}"
  if [ -x "$MYSQL_SERVER" ]; then
    echo "启动 MySQL：$MYSQL_SERVER start"
    "$MYSQL_SERVER" start >/dev/null 2>&1 || true
    i=0
    while [ "$i" -lt 20 ]; do
      mysqladmin ping --silent >/dev/null 2>&1 && break
      i=$((i + 1)); sleep 1
    done
  else
    echo "warn: 未找到 mysqladmin 或 $MYSQL_SERVER；患者画像将降级（degraded.profile=true）"
  fi
fi
if command -v mysqladmin >/dev/null 2>&1 && mysqladmin ping --silent >/dev/null 2>&1; then
  echo "初始化 dermagent 库表（幂等）…"
  PYTHONPATH="$SERVER${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c \
    "from memory.profile import MySQLProfileStore; s=MySQLProfileStore(); print(s.health())" \
    || echo "warn: 建库失败（检查 pymysql 与 ~/.my.cnf）；患者画像降级"
else
  echo "warn: MySQL 未就绪；跳过建库"
fi

echo "== 3/3 Milvus =="
COMPOSE="$ROOT/deploy/milvus/docker-compose.yml"
if [ -f "$COMPOSE" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "docker compose up -d（Milvus Standalone）"
  (cd "$ROOT/deploy/milvus" && docker compose up -d) || echo "warn: Milvus 启动失败；情景记忆降级"
else
  echo "warn: Docker 守护进程未运行或缺少 $COMPOSE；情景记忆降级（degraded.episodic=true）"
  echo "      先启动 Docker Desktop，再执行：cd deploy/milvus && docker compose up -d"
fi

echo
echo "服务端启动后可查：curl -s http://127.0.0.1:${PORT:-8765}/api/memory/health"
