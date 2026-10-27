#!/usr/bin/env bash
# 制作标准「拖拽安装」布局的 ThinkBuddy DMG：
#   挂载卷里 = ThinkBuddy.app + 指向 /Applications 的符号链接，
#   用户双击 dmg 后直接把 app 拖进 Applications 即完成安装。
#
# 命名与 Windows 对齐：ThinkBuddy-mac-<arch>-dt<版本>.dmg
#   注：dmg 文件名不变，仅内部的 .app 改叫 ThinkBuddy.app，
#   app 名与 Windows 桌面快捷方式一致显示为 ThinkBuddy。
# 版本来自 inject_version 写入 desktop/__init__.py 的 __version__（<引擎版本>.<MMDDHHmm>），
# 本脚本不再自行计算时间戳，保证 plist / 文件名 / 清单三处一致。
#
# Usage:
#   bash tools/make_dmg.sh [app_path] [output_dmg]
#   默认: dist/ThinkBuddy.app -> dist/ThinkBuddy-mac-<arch>-dt<ver>.dmg
set -euo pipefail

cd "$(dirname "$0")/.."
APP="${1:-dist/ThinkBuddy.app}"
ARCH=$(uname -m)

# 壳版本（inject_version 已注入 desktop/__init__.py；格式 <引擎版本>.<MMDDHHmm>）
SHELL_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' desktop/__init__.py | head -1)
if printf '%s' "$SHELL_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]{8}$'; then
  APP_VERSION="$SHELL_VERSION"
else
  echo "ERROR: desktop/__init__.py __version__=$SHELL_VERSION 不符合四段/末段8位；请先 inject_version" >&2
  exit 1
fi
DEFAULT_OUT="dist/ThinkBuddyDesktop-mac-${ARCH}-dt${APP_VERSION}.dmg"

OUT="${2:-$DEFAULT_OUT}"

[ -d "$APP" ] || { echo "app 不存在: $APP" >&2; exit 1; }
[ -e "$OUT" ] && rm -f "$OUT"

STAGING="$(mktemp -d "${TMPDIR:-/tmp}/thinkbuddy-dmg.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT

cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# 用「显式挂载 + 拷贝 + 卸载」替代 hdiutil -srcfolder：
STAGING_DMG="$(mktemp "${TMPDIR:-/tmp}/thinkbuddy-dmg-src.XXXXXX").dmg"
trap 'rm -f "$STAGING_DMG"; rm -rf "$STAGING"' EXIT

# 1) 创建空镜像（默认即 UDRW 读写格式，最终转 UDZO 压缩）
#    -fs HFS+ 创建文件系统卷，-size 预留空间避免拷贝中途写满。
#    注意：-format 只允许与 -srcfolder/-srcdevice（或 convert）搭配；
#    用 -size 创建空镜像时带上 -format，hdiutil 会直接报
#    “-format requires -srcfolder or -srcdevice”（macOS 15 / ARM runner 上必现）。
VOLSIZE_KB=$(du -sk "$STAGING" | awk '{print $1}')
# HFS+ 实际占用远大于 du 的源大小：卷头/日志/Catalog 树 + 每个文件按 4KB
# 分配块取整（Frameworks/python3.12 有数万小文件，开销可观）+ 资源叉/xattr。
# 10% 余量在大包（runtime.zip + python3.12 框架）上实测 ENOSPC（arm CI 实证，
# 2026-10-27）。按 2 倍 + 512MiB 预留，宁可大不可小——最终转 UDZO 压缩，
# 产物体积不受中间镜像大小影响。
VOLSIZE_KB=$(( VOLSIZE_KB * 2 + 524288 ))
hdiutil create -volname "ThinkBuddy" -fs HFS+ -size "${VOLSIZE_KB}k" \
  -ov "$STAGING_DMG"

# 2) 挂载空镜像（-noidme 禁用 Spotlight 索引，避免挂载卷被占用）
MOUNT_POINT="$(hdiutil attach "$STAGING_DMG" -nobrowse -noidme -readwrite \
  | awk -F'\t' '/\/Volumes\//{print $NF; exit}')"
trap 'hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true; rm -f "$STAGING_DMG"; rm -rf "$STAGING"' EXIT

# 3) 拷贝内容到挂载卷（源目录此时不被任何镜像进程锁定）
ditto "$STAGING/" "$MOUNT_POINT/"

# 4) 卸载，镜像落盘
hdiutil detach "$MOUNT_POINT" -quiet

# 5) 转为最终压缩 DMG（UDZO）。
#    hdiutil convert 会自动给输出名补 .dmg 后缀；这里用不带 .dmg 的目标基名，
#    转换产物即 $OUT（脚本开头已 rm 掉旧 $OUT，避免 convert 因目标存在而拒绝覆盖）。
rm -f "$OUT"
hdiutil convert "$STAGING_DMG" -ov -format UDZO -o "${OUT%.dmg}"

# ---- 生成 mac 更新清单 latest-mac-<arch>.json（与 Windows 的 latest.json 同契约）----
# 上传到更新源目录（UPDATE_BASE）后，mac 客户端经统一 latest.json 的
# mac.<arch> 子对象（CI release job 汇总）或本片段直接消费。
UPDATE_BASE="${UPDATE_BASE:-https://thinkbuddy.hanyoai.com/update}"
MIN_VERSION="${MIN_VERSION:-0.1.0}"
DMG_NAME=$(basename "$OUT")
SHA256=$(shasum -a 256 "$OUT" | awk '{print $1}')
SIZE=$(stat -f%z "$OUT")
PUBLISHED=$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S+08:00)

cat > "dist/latest-mac-${ARCH}.json" <<EOF
{
  "app": "ThinkBuddy",
  "platform": "darwin",
  "arch": "${ARCH}",
  "version": "${APP_VERSION}",
  "min_version": "${MIN_VERSION}",
  "url": "${UPDATE_BASE%/}/${DMG_NAME}",
  "sha256": "${SHA256}",
  "size": ${SIZE},
  "published_at": "${PUBLISHED}",
  "changelog": ""
}
EOF

echo "DMG 完成: $OUT"
echo "更新清单: dist/latest-mac-${ARCH}.json"
