#!/usr/bin/env bash
# 制作标准「拖拽安装」布局的 ThinkBuddy DMG：
#   挂载卷里 = ThinkBuddyDesktop.app + 指向 /Applications 的符号链接，
#   用户双击 dmg 后直接把 app 拖进 Applications 即完成安装。
#
# 命名与 Windows 对齐：ThinkBuddyDesktop-mac-<arch>-dt<版本>.<MMddHHmm>.dmg
# （引擎版本读 deeptutor/__version__.py；时间戳取北京时间，与 build.ps1 同源）。
#
# Usage:
#   bash tools/make_dmg.sh [app_path] [output_dmg]
#   默认: dist/ThinkBuddyDesktop.app -> dist/ThinkBuddyDesktop-mac-<arch>-dt<ver>.<ts>.dmg
set -euo pipefail

cd "$(dirname "$0")/.."
APP="${1:-dist/ThinkBuddyDesktop.app}"
ARCH=$(uname -m)

# 引擎版本（与 inject_version/build.ps1 同源：deeptutor/__version__.py）
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../deeptutor/__version__.py | head -1)
[ -n "$VERSION" ] || VERSION="0.0.0"
# 北京时间 MMddHHmm（CI runner 时区不定，必须显式指定）
TIMESTAMP=$(TZ=Asia/Shanghai date +%m%d%H%M)
DEFAULT_OUT="dist/ThinkBuddyDesktop-mac-${ARCH}-dt${VERSION}.${TIMESTAMP}.dmg"

OUT="${2:-$DEFAULT_OUT}"

[ -d "$APP" ] || { echo "app 不存在: $APP" >&2; exit 1; }
[ -e "$OUT" ] && rm -f "$OUT"

STAGING="$(mktemp -d /tmp/thinkbuddy-dmg.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "ThinkBuddy" -srcfolder "$STAGING" -ov -format UDZO "$OUT"

# ---- 生成 mac 更新清单 latest-mac-<arch>.json（与 Windows 的 latest.json 同契约）----
# 上传到更新源目录（UPDATE_BASE）后，mac 客户端经统一 latest.json 的
# mac.<arch> 子对象（CI release job 汇总）或本片段直接消费。
UPDATE_BASE="${UPDATE_BASE:-https://thinkbuddy.hanyoai.com/update}"
MIN_VERSION="${MIN_VERSION:-0.1.0}"
DMG_NAME=$(basename "$OUT")
SHA256=$(shasum -a 256 "$OUT" | awk '{print $1}')
SIZE=$(stat -f%z "$OUT")
PUBLISHED=$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S+08:00)
APP_VERSION="${VERSION}.${TIMESTAMP}"

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
