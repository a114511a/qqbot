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

# 优先使用独立的 docker-compose 可执行文件，而不是 `docker compose` 子命令。
#
# 原因：在某些 Terminal / 非交互环境下，`docker` 二进制找不到 CLI 插件目录，
# `docker compose` 会退化成 `docker` 主命令，报 "unknown flag: --env-file"；
# 而 cli-plugins/docker-compose 是自包含的，不受插件查找逻辑影响，更稳。
COMPOSE_BIN=""
for candidate in \
  "/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose" \
  "$HOME/.docker/cli-plugins/docker-compose" \
  "/opt/homebrew/bin/docker-compose" \
  "/usr/local/bin/docker-compose"; do
  if [ -x "$candidate" ]; then
    COMPOSE_BIN="$candidate"
    break
  fi
done

if [ -z "$COMPOSE_BIN" ]; then
  pause_and_exit 1 "没有找到 docker compose。请确认 Docker Desktop 已安装且包含 compose 插件。"
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  pause_and_exit 1 "找不到项目 .env 文件：$PROJECT_DIR/.env"
fi

printf '已找到 Docker：%s\n' "$DOCKER_BIN"
printf '正在检查 Docker Desktop 后台服务……\n'

# 探测 Docker 后台是否就绪。
#
# ⚠ 这里刻意用 `docker version` 而不是 `docker info`：
# 实测本机 Docker Desktop 4.95 上 `docker info --format` 会无限期挂起
# （`docker version` 只要 0.08 秒），导致脚本一直卡在「正在检查」这一步。
# 两者都能证明「后台引擎可用」，用 version 更稳。
#
# 超时用 timeout(1) 实现（macOS 需 coreutils；不存在时退回 perl alarm）。
# 注意 perl 的 alarm 对已 exec 的 docker 未必生效（曾出现探测进程杀掉后
# 子进程仍挂着的情况），所以优先用真正的 timeout。
_TIMEOUT_BIN=""
for candidate in /opt/homebrew/bin/timeout /usr/local/bin/timeout /usr/bin/timeout; do
  if [ -x "$candidate" ]; then
    _TIMEOUT_BIN="$candidate"
    break
  fi
done

_run_with_timeout() {
  local seconds="$1"
  shift
  if [ -n "$_TIMEOUT_BIN" ]; then
    "$_TIMEOUT_BIN" -k 2 "$seconds" "$@"
  else
    /usr/bin/perl -e 'alarm shift; exec @ARGV' "$seconds" "$@"
  fi
}

docker_server_ready() {
  _run_with_timeout 5 "$DOCKER_BIN" version --format '{{.Server.Version}}' >/dev/null 2>&1
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

printf 'Docker 后台服务已就绪。\n'
printf '确认镜像是否已存在（已存在则不联网拉取）……\n'

# ⚠ `compose up -d` 默认会先向 Docker Hub 校验镜像是否有更新。
# 国内网络下 registry-1.docker.io 常常完全不可达（实测 15 秒无响应），
# compose 会一直卡在这一步，表现为「正在启动 NapCat 容器」后无限等待。
#
# 处理策略：
#   1. 本地已有镜像 → 加 --pull never，完全不碰网络；
#   2. 本地没有镜像 → 才允许拉取，并带上超时与明确提示。
IMAGE_NAME="mlikiowa/napcat-docker:latest"
if "$DOCKER_BIN" image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  PULL_ARGS=(--pull never)
  printf '镜像已存在，跳过联网检查。\n'
else
  PULL_ARGS=()
  printf '本地没有 %s，需要联网拉取（国内网络可能较慢或失败）。\n' "$IMAGE_NAME"
fi

printf '正在启动 NapCat 容器……\n'
if ! _run_with_timeout 180 "$COMPOSE_BIN" --env-file "$PROJECT_DIR/.env" \
      -f "$PROJECT_DIR/compose.yaml" up -d "${PULL_ARGS[@]}" napcat; then
  pause_and_exit 1 "NapCat 启动失败或超时。
常见原因：
  1. 本地没有镜像且无法访问 Docker Hub —— 请先在能联网的环境 docker pull $IMAGE_NAME
  2. Docker Desktop 仍未完全就绪 —— 等界面显示 Running 后再双击本脚本
可手动排查：
  $COMPOSE_BIN --env-file \"$PROJECT_DIR/.env\" -f \"$PROJECT_DIR/compose.yaml\" up -d napcat"
fi

printf '\nNapCat 已启动。WebUI： http://127.0.0.1:6099/webui\n'
printf '使用 .env 中的 NAPCAT_WEBUI_TOKEN 登录 WebUI，然后扫码登录 QQ。\n'
printf '以下为 NapCat 实时日志；按 Control-C 只会停止查看日志，不会停止容器。\n\n'
"$COMPOSE_BIN" --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml" logs -f napcat
