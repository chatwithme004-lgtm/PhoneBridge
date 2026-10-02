#!/bin/bash
# Download the two helper programs PhoneBridge bundles into tools/:
#   adb    - Android SDK Platform-Tools (only used when a phone has USB debugging on)
#   ffmpeg - used to make video thumbnails
set -e
cd "$(dirname "$0")/.."
mkdir -p tools
tmp=$(mktemp -d)

echo "==> Android SDK Platform-Tools (adb)"
curl -fL -o "$tmp/pt.zip" https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
rm -rf tools/platform-tools && unzip -q "$tmp/pt.zip" -d tools/

echo "==> FFmpeg (Apple Silicon static build)"
curl -fL -o "$tmp/ffmpeg.zip" https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip
unzip -q -o "$tmp/ffmpeg.zip" -d tools/

chmod +x tools/ffmpeg tools/platform-tools/adb
xattr -dr com.apple.quarantine tools 2>/dev/null || true
rm -rf "$tmp"
echo "==> Done. tools/ now has adb and ffmpeg."
