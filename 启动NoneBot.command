#!/bin/bash
set -u

# 启动 NoneBot 机器人。
# 会先检查 .env 里 PORT 指定的端口是否被占用：如果占用的是本项目的旧进程，
# 会询问是否结束它再启动。否则新进程只会报 "address already in use" 然后静默退出，
# 看起来像「机器人启动不了 / 指令没反应」。

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

pause_and_exit() {
  local code="$1"
  printf '\n%s\n' "$2"
  printf '按回车键关闭窗口。'
  read -r _ || true
  exit "$code"
}

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  pause_and_exit 1 "找不到项目虚拟环境：$PROJECT_DIR/.venv/bin/python
请先在项目目录安装依赖：uv sync"
fi

# 从 .env 读端口；读不到就按 NoneBot 默认的 8080 处理。
PORT="$(grep -E '^PORT=' "$PROJECT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
[ -n "$PORT" ] || PORT=8080

if command -v lsof >/dev/null 2>&1; then
  STALE_PIDS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -n "$STALE_PIDS" ]; then
    printf '端口 %s 已被占用，占用进程：\n' "$PORT"
    for pid in $STALE_PIDS; do
      # 显示进程的工作目录，方便确认是不是本项目的旧实例。
      # lsof 会把非 ASCII 路径转义成 \xNN，用 printf %b 还原成中文。
      info="$(lsof -p "$pid" 2>/dev/null | awk '$4 == "cwd" {print $NF}')"
      [ -n "$info" ] && info="$(printf '%b' "$info")"
      printf '  PID %s  工作目录 %s\n' "$pid" "${info:-未知}"
    done
    printf '\n如果这是上次没关掉的机器人，需要先结束它才能启动新的。\n'
    printf '要结束以上进程并继续吗？(y/N) '
    read -r answer || answer=""
    case "$answer" in
      y|Y|yes|YES)
        for pid in $STALE_PIDS; do
          kill -TERM "$pid" 2>/dev/null || true
        done
        for _ in {1..10}; do
          sleep 0.5
          [ -z "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ] && break
        done
        for pid in $STALE_PIDS; do
          if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
          fi
        done
        sleep 1
        if [ -n "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ]; then
          pause_and_exit 1 "端口 $PORT 仍被占用，请手动处理后重试。"
        fi
        printf '已释放端口 %s。\n' "$PORT"
        ;;
      *)
        pause_and_exit 1 "已取消。请先关闭占用端口 $PORT 的程序，再双击本脚本。"
        ;;
    esac
  fi
fi

export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
printf '正在启动 NoneBot，监听 0.0.0.0:%s，日志见下方。\n' "$PORT"
printf '按 Control-C 可停止机器人。\n\n'
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/bot.py"
