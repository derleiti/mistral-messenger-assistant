#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../client"
if ! command -v flutter >/dev/null 2>&1; then
  echo "Flutter SDK not found. Install Flutter first, then rerun this script." >&2
  exit 1
fi
# Generate native platform runners without replacing our lib/pubspec.
cp lib/main.dart /tmp/mistral_messenger_main.dart
cp pubspec.yaml /tmp/mistral_messenger_pubspec.yaml
flutter create --platforms=android,linux,windows --project-name mistral_messenger_assistant .
cp /tmp/mistral_messenger_main.dart lib/main.dart
cp /tmp/mistral_messenger_pubspec.yaml pubspec.yaml
flutter pub get
flutter analyze
