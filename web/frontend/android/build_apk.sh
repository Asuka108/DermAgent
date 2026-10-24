#!/bin/bash
# DermAgent 安卓端 APK 命令行构建（零 Gradle：aapt2 + javac + d8 + zipalign + apksigner）
# 产物：client/web/dermagent.apk（debug 签名，手机可直接安装）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$HOME/derm-build-tools"
SDK="$TOOLS/android-sdk"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JH="$(ls -d "$TOOLS"/jdk-*/Contents/Home 2>/dev/null | head -1)"
[ -x "$JH/bin/javac" ] || { echo "JDK 未安装于 $TOOLS"; exit 1; }
[ -f "$PLAT" ] || { echo "Android platform 34 未安装于 $SDK"; exit 1; }
export PATH="$JH/bin:$PATH"

BUILD="$ROOT/build"
rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"
cd "$ROOT"

echo "[1/6] aapt2 compile"
"$BT/aapt2" compile --dir res -o "$BUILD/res.zip"
echo "[2/6] aapt2 link"
"$BT/aapt2" link -o "$BUILD/base.apk" -I "$PLAT" --manifest AndroidManifest.xml \
  --min-sdk-version 24 --target-sdk-version 34 --version-code 1 --version-name 1.0 \
  --java "$BUILD/gen" "$BUILD/res.zip"
echo "[3/6] javac"
"$JH/bin/javac" -source 8 -target 8 -Xlint:-options -classpath "$PLAT" \
  -d "$BUILD/classes" "$BUILD/gen/cn/dermagent/app/R.java" java/cn/dermagent/app/MainActivity.java
echo "[4/6] d8 (dex)"
"$BT/d8" --release --lib "$PLAT" --output "$BUILD/dex" \
  $(find "$BUILD/classes" -name '*.class' | tr '\n' ' ')
(cd "$BUILD/dex" && zip -q -j "$BUILD/base.apk" classes.dex)
echo "[5/6] zipalign"
"$BT/zipalign" -f 4 "$BUILD/base.apk" "$BUILD/aligned.apk"
echo "[6/6] apksigner"
KS="$TOOLS/derm-debug.keystore"
[ -f "$KS" ] || "$JH/bin/keytool" -genkeypair -keystore "$KS" -alias derm \
  -storepass dermagent -keypass dermagent -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=DermAgent Debug,O=DermAgent,C=CN"
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:dermagent --key-pass pass:dermagent \
  --out "$ROOT/../web/dermagent.apk" "$BUILD/aligned.apk"
ls -lh "$ROOT/../web/dermagent.apk"
echo "OK：client/web/dermagent.apk（手机访问 http://<电脑IP>:8765/dermagent.apk 下载安装）"
