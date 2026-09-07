# Aira Android app

This directory contains the Android-only Flutter client for Aira, a clearly
identified AI voice companion. `Aira` is a temporary product name centralized
in `lib/core/constants/app_copy.dart`.

The current local prototype connects only Aanya to the trusted-development Aira
HTTP service for push-to-talk voice turns. The other 19 companions retain their
deterministic mock-call experience. Visible Aanya turns are screen-session state
only; the backend does not yet provide persistent conversational memory.

The app also contains a bounded, version-1 realtime WebSocket client and typed
state/protocol handling. It is covered with fake-socket tests but is not yet
wired to the production call screen, microphone PCM streaming, or continuous
PCM playback. The existing HTTP Aanya experience remains the active UI until a
realtime inference stack is fast enough and device measurements are available.

The development API is unauthenticated and uses plain HTTP. Use it only on a
trusted LAN, never port-forward it to the public internet, and do not use it on
untrusted Wi-Fi. Android cleartext access is enabled only for debug builds;
release builds retain Android's secure default and therefore require an HTTPS
backend URL.

## Run locally

From this directory, install packages and run on a selected Android device:

```powershell
flutter pub get
flutter run -d <DEVICE_ID> --dart-define=AIRA_API_BASE_URL=http://192.168.1.100:8765
```

`AIRA_API_BASE_URL` is validated and normalized in one configuration class. The
fallback is the current development host shown above, but `--dart-define` should
be used whenever the laptop's LAN address changes.

Run the local checks with:

```sh
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build apk --debug --dart-define=AIRA_API_BASE_URL=http://192.168.1.100:8765
```

Opening Aanya performs one short-timeout health check. A voice turn records a
unique 16 kHz mono WAV in the app cache, posts it once as multipart form data,
deletes the microphone WAV, displays the normalized transcript and response,
then plays the returned same-origin WAV without permanently saving it.
