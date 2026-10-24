#!/bin/bash
# 肤智 DermAgent —— macOS/Linux 链接重建脚本
# 用途：zip/网盘/跨文件系统拷贝后软链丢失、或变成 0 字节占位文件时，在本目录跑一次即可。
set -uo pipefail
cd "$(dirname "$0")"
mk() { # $1=链接路径  $2=目标（相对链接自身所在目录）
  if [ -L "$1" ]; then echo "已存在（软链） $1"; return; fi
  if [ -d "$1" ]; then echo "跳过（是真实目录，未动） $1"; return; fi
  if [ -f "$1" ] && [ ! -s "$1" ]; then mv "$1" "$1.placeholder-bak"; echo "移走 0 字节占位文件 $1 -> $1.placeholder-bak"; fi
  mkdir -p "$(dirname "$1")"; ln -sfn "$2" "$1"; echo "已建 $1 -> $2"
}
mk win/frontend/web              ../../web/frontend
mk win/frontend/agent_client.py  ../backend/agent_client.py
mk win/backend/shared            ../../web/backend/shared
mk win/backend/server            ../../web/backend/server
mk win/shared                    backend/shared
mk win/server                    backend/server
mk win/client/web                ../frontend/web
mk win/webui                     ../web/frontend/legacy-webui
mk web/backend/client/web        ../../frontend
mk web/backend/webui             ../frontend/legacy-webui
echo "—— 可达性校验 ——"
for f in win/frontend/web/index.html win/server/app.py win/shared/derm_core.py \
         web/backend/client/web/main.js web/backend/shared/derm_core.py; do
  if [ -f "$f" ]; then echo "  OK   $f"; else echo "  FAIL $f"; fi
done
