#!/bin/bash
set -u

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

pause_and_exit() {
  local code="$1"
  printf '\n%s\n' "$2"
  printf '按回车键关闭窗口。'
  read -r _ || true
  exit "$code"
}

# Finder 启动的 Terminal 不一定继承交互式 shell 的 PATH。
export PATH="$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

DOCKER_BIN=""
for candidate in \
  "/Applications/Docker.app/Contents/Resources/bin/docker" \
  "$HOME/.docker/bin/docker" \
  "/opt/homebrew/bin/docker" \
  "/usr/local/bin/docker"; do
  if [ -x "$candidate" ]; then
    DOCKER_BIN="$candidate"
    break
  fi
done

if [ -z "$DOCKER_BIN" ]; then
  pause_and_exit 1 "没有找到 Docker CLI。请确认 Docker Desktop 已安装在“应用程序”文件夹中。"
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  pause_and_exit 1 "找不到项目 .env 文件：$PROJECT_DIR/.env"
fi

printf '已找到 Docker：%s\n' "$DOCKER_BIN"
printf '正在检查 Docker Desktop 后台服务……\n'

# 给每次探测设置上限，避免 Docker 尚未启动时脚本静默卡住。
docker_server_ready() {
  /usr/bin/perl -e 'alarm 3; exec @ARGV' \
    "$DOCKER_BIN" info --format '{{.ServerVersion}}' >/dev/null 2>&1
}

if ! docker_server_ready; then
  if [ ! -d "/Applications/Docker.app" ]; then
    pause_and_exit 1 "Docker Desktop 尚未运行，且未找到 /Applications/Docker.app。请先安装并启动 Docker Desktop。"
  fi

  printf '正在打开 Docker Desktop，并等待后台服务启动（最多约 2 分钟）……\n'
  open -a "/Applications/Docker.app" >/dev/null 2>&1 || true

  ready=0
  for attempt in {1..30}; do
    if docker_server_ready; then
      ready=1
      break
    fi
    printf '.'
    sleep 1
  done
  printf '\n'

  if [ "$ready" -ne 1 ]; then
    pause_and_exit 1 "Docker 后台服务仍未就绪。请查看 Docker Desktop 是否在等待接受许可或系统授权；等界面显示运行后，再双击本脚本。"
  fi
fi

printf 'Docker 后台服务已就绪。\n正在启动 NapCat 容器……\n'
if ! "$DOCKER_BIN" compose --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml" up -d napcat; then
  pause_and_exit 1 "NapCat 启动失败，请查看上方错误信息。"
fi

printf '\nNapCat 已启动。WebUI： http://127.0.0.1:6099/webui\n'
printf '使用 .env 中的 NAPCAT_WEBUI_TOKEN 登录 WebUI，然后扫码登录 QQ。\n'
printf '以下为 NapCat 实时日志；按 Control-C 只会停止查看日志，不会停止容器。\n\n'
"$DOCKER_BIN" compose --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml" logs -f napcat
