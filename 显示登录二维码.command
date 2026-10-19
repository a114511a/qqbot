#!/bin/bash
set -u

# 显示 NapCat 的 QQ 登录二维码。
# 二维码有效期很短（约 1~2 分钟），过期后需要重启容器重新生成，所以本脚本会先看现有
# 二维码是否新鲜；过期时才询问是否重启容器（重启会短暂断开机器人）。
# 机器人已经在线时不要运行本脚本。

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

OUT_FILE="$PROJECT_DIR/outputs/napcat-login-qrcode.png"
COMPOSE_ARGS=(--env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml")
QR_MAX_AGE=110  # 秒。超过这个时间就认为二维码可能已失效。

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
[ -n "$DOCKER_BIN" ] || pause_and_exit 1 "没有找到 Docker CLI。请确认 Docker Desktop 已安装。"

# 取容器内 qrcode.png 的修改时间（epoch 秒），顺便判断容器是否在跑。
qr_age_seconds() {
  local mtime
  mtime="$("$DOCKER_BIN" exec napcat stat -c %Y /app/napcat/cache/qrcode.png 2>/dev/null)" || return 1
  [ -n "$mtime" ] || return 1
  echo $(( $(date +%s) - mtime ))
}

# 把二维码复制出来并用预览打开；同时打印日志里的解码 URL 备用。
show_qr() {
  mkdir -p "$PROJECT_DIR/outputs"
  if ! "$DOCKER_BIN" cp napcat:/app/napcat/cache/qrcode.png "$OUT_FILE" >/dev/null 2>&1; then
    return 1
  fi
  open "$OUT_FILE" >/dev/null 2>&1 || true
  printf '二维码已保存并打开：%s\n' "$OUT_FILE"
  local url
  url="$("$DOCKER_BIN" logs --tail 300 napcat 2>&1 | grep '二维码解码URL' | tail -1 | sed 's/.*二维码解码URL: //')"
  if [ -n "$url" ]; then
    printf '\n如果图片扫不出来，可以把下面这行复制到任意二维码生成网站再扫：\n%s\n' "$url"
  fi
}

if ! "$DOCKER_BIN" ps --format '{{.Names}}' 2>/dev/null | grep -qx napcat; then
  pause_and_exit 1 "NapCat 容器没有在运行。请先双击「启动NapCat.command」。"
fi

age="$(qr_age_seconds)" || pause_and_exit 1 "容器里还没有二维码。请双击「启动NapCat.command」，等终端出现二维码后再试。"

if [ "$age" -le "$QR_MAX_AGE" ]; then
  printf '二维码是 %s 秒前生成的，应该还能用。\n' "$age"
  show_qr || pause_and_exit 1 "复制二维码失败，请查看上方错误。"
  printf '\n用手机 QQ 扫码登录即可。\n'
  pause_and_exit 0 "登录成功后，双击「启动NoneBot.command」启动机器人。"
fi

printf '现有二维码是 %s 秒前生成的，很可能已经过期。\n' "$age"
printf '\n重新生成二维码需要重启 NapCat 容器：\n'
printf '  - 机器人会断开约半分钟；\n'
printf '  - 如果 QQ 当前已登录，重启后登录态会失效，必须重新扫码。\n'
printf '机器人现在正常在线的话，请不要继续。\n\n'
printf '要重启容器并显示新二维码吗？(y/N) '
read -r answer || answer=""
case "$answer" in
  y|Y|yes|YES) ;;
  *) pause_and_exit 0 "已取消，没有改动任何东西。" ;;
esac

printf '\n正在重启 NapCat 容器……\n'
"$DOCKER_BIN" compose "${COMPOSE_ARGS[@]}" restart napcat || pause_and_exit 1 "重启失败，请查看上方错误。"

printf '等待二维码生成……\n'
for _ in {1..40}; do
  sleep 2
  if age="$(qr_age_seconds)" && [ "$age" -le 20 ]; then
    show_qr || pause_and_exit 1 "复制二维码失败，请查看上方错误。"
    printf '\n用手机 QQ 扫码登录即可。\n'
    pause_and_exit 0 "登录成功后，双击「启动NoneBot.command」启动机器人。"
  fi
  printf '.'
done

pause_and_exit 1 "等了约 80 秒仍没等到新二维码。请双击「启动NapCat.command」查看终端日志里的报错。"
