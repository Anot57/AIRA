# Aira Android app

This directory contains the Android-only Flutter client for Aira, a clearly
identified AI voice companion. `Aira` is a temporary product name centralized
in `lib/core/constants/app_copy.dart`.

The current milestone is a completely local mock experience. Onboarding,
scheduling, theme selection, memory consent, and simulated call state live only
in memory. The app does not use a backend, microphone, authentication, payment,
or network service.

## Run locally

From this directory:

```sh
flutter run
```

Run the local checks with:

```sh
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
```
