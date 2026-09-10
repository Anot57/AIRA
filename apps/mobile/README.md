# Aira Android app

This directory contains the Android-only Flutter client for Aira, a clearly
identified AI voice companion. `Aira` is a temporary product name centralized
in `lib/core/constants/app_copy.dart`.

The current local prototype connects only Aanya to the trusted-development Aira
realtime WebSocket for a continuous Start Call / End Call conversation. The
original whole-WAV HTTP
controller remains available as a fallback/debug path. The other 19 companions
retain their deterministic mock-call experience. Visible Aanya turns are
screen-session state only; there is no persistent conversational memory.

The production Aanya screen uses one bounded, version-1 WebSocket per call
session. Android microphone input uses a native `AudioRecord` read loop at
16 kHz mono PCM16, preferring `VOICE_RECOGNITION` and falling back to `MIC`.
The `record` package remains only for the existing permission flow and the HTTP
whole-WAV fallback; production realtime capture does **not** call
`record.startStream`. Native frames are defensively rechunked to 1,280 bytes
(40 ms).
Response PCM is forwarded in sequence to a native Android `AudioTrack` in
streaming mode at the server-announced rate; playback begins with the first
validated chunk rather than waiting for `turn_complete`.

After Start Call, listening begins automatically. An energy endpoint detector
requires three consecutive onset frames, uses adaptive-noise hysteresis, and
finalizes exactly once after 3,000 ms of post-speech silence. It never creates
empty turns before confirmed speech. A bounded 400 ms pre-speech ring preserves
soft first syllables; only 200 ms of trailing padding is forwarded, so the
intentional endpoint wait does not add roughly three seconds of known silence
to batch STT. The threshold can be overridden at build time with
`AIRA_USER_SILENCE_ENDPOINT_MS`.

The realtime socket, session handshake, and safe audio-format preparation are
warmed when the screen opens. A 950 ms client watchdog and 900 ms native
watchdog turn a zero-byte capture into a recoverable error. Endpoint teardown
waits for the native read loop's final frame, Dart framing, and ordered
WebSocket writes before `end_of_turn`.

An Android microphone foreground service owns active-call intent and native
audio resources. Home, shade, lock/screen-off, volume-key UI, app switching,
and transient audio focus changes do not intentionally close the call or
socket. Network loss enters reconnecting and preserves active-call intent.
Android may still suspend or revoke audio for a phone call, alarm, exclusive
microphone owner, process kill, or force-stop; those conditions cannot be
prevented by an app.

Internal cancellation stops native capture and playback before waiting for the server. The
client accepts only the matching `turn_cancelled` acknowledgement, suppresses
stale text/audio while cancellation is pending, and recreates the WebSocket
session after a bounded 400 ms acknowledgement timeout. Leaving the call also
invalidates queued callbacks and stops the engine-owned native bridges; opening
it again creates a new controller, client, socket, and protocol state. Native
channel owners remain alive until Flutter engine cleanup, so disposing one
screen cannot permanently poison the next screen instance.

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

Opening Aanya creates and prewarms a persistent realtime session. Start Call
keeps the microphone disabled until Android permission, foreground-service
startup, and `session_ready` with `can_process_turns=true`. The HTTP
fallback still records a temporary 16 kHz mono WAV, posts it once, and plays the
returned same-origin WAV without permanently saving it.
