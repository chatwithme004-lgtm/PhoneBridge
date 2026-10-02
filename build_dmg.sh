#!/bin/bash
# Build PhoneBridge.app with PyInstaller, then package it into PhoneBridge.dmg.
set -e
cd "$(dirname "$0")"

echo "==> Cleaning previous build"
rm -rf build dist PhoneBridge.dmg dmg_staging

echo "==> Building PhoneBridge.app (PyInstaller)"
python3 -m PyInstaller --noconfirm --windowed --name PhoneBridge \
  --icon PhoneBridge.icns \
  --osx-bundle-identifier io.github.chatwithme004-lgtm.phonebridge \
  --add-data "templates:templates" \
  --add-data "tools/platform-tools/adb:tools/platform-tools" \
  --add-data "tools/ffmpeg:tools" \
  --collect-all webview \
  --collect-all libusb_package \
  --hidden-import webview.platforms.cocoa \
  --hidden-import usb.backend.libusb1 \
  --hidden-import segno \
  --exclude-module PyQt5 --exclude-module PyQt6 \
  --exclude-module PySide2 --exclude-module PySide6 \
  --exclude-module webview.platforms.qt \
  --exclude-module webview.platforms.gtk \
  --exclude-module tkinter --exclude-module matplotlib \
  --exclude-module IPython --exclude-module jedi \
  launcher.py

echo "==> App details (version, Wi-Fi permission text, dark mode)"
PLIST=dist/PhoneBridge.app/Contents/Info.plist
plutil -replace CFBundleShortVersionString -string "2.0" "$PLIST"
plutil -replace CFBundleVersion -string "2.0" "$PLIST"
plutil -replace NSLocalNetworkUsageDescription -string "PhoneBridge uses your Wi-Fi so your phone can send and receive files after scanning the QR code." "$PLIST"
plutil -replace NSRequiresAquaSystemAppearance -bool false "$PLIST"

echo "==> Ad-hoc code-signing (required to run on Apple Silicon)"
codesign --force --deep --sign - dist/PhoneBridge.app || true

echo "==> Staging DMG"
mkdir -p dmg_staging
cp -R dist/PhoneBridge.app dmg_staging/
ln -s /Applications dmg_staging/Applications

echo "==> Creating PhoneBridge.dmg"
hdiutil create -volname "PhoneBridge" -srcfolder dmg_staging \
  -ov -format UDZO PhoneBridge.dmg

rm -rf dmg_staging
echo "==> Done:  $(pwd)/PhoneBridge.dmg"
ls -lh PhoneBridge.dmg
