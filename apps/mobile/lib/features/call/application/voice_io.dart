import 'dart:async';
import 'dart:typed_data';

enum ActiveCallPlatformEventType {
  state,
  foregroundServiceStarted,
  audioFocus,
  endRequested,
  ended,
}

final class ActiveCallPlatformEvent {
  const ActiveCallPlatformEvent({
    required this.type,
    required this.generation,
    required this.monotonicNanos,
    required this.active,
    this.state,
    this.audioFocus,
    this.reason,
  });

  final ActiveCallPlatformEventType type;
  final int generation;
  final int monotonicNanos;
  final bool active;
  final String? state;
  final String? audioFocus;
  final String? reason;
}

abstract interface class ActiveCallPlatform {
  Stream<ActiveCallPlatformEvent> get events;

  Future<void> startCall({required int generation});

  Future<void> updateState({
    required int generation,
    required String state,
    String? sessionId,
  });

  Future<void> endCall({required int generation});

  /// Detaches this Dart adapter without changing native call intent.
  Future<void> dispose();
}

/// Deterministic non-Android/test fallback. Production explicitly injects the
/// foreground-service adapter.
final class NoopActiveCallPlatform implements ActiveCallPlatform {
  const NoopActiveCallPlatform();

  @override
  Stream<ActiveCallPlatformEvent> get events => const Stream.empty();

  @override
  Future<void> startCall({required int generation}) async {}

  @override
  Future<void> updateState({
    required int generation,
    required String state,
    String? sessionId,
  }) async {}

  @override
  Future<void> endCall({required int generation}) async {}

  @override
  Future<void> dispose() async {}
}

final class RealtimeMicrophoneException implements Exception {
  const RealtimeMicrophoneException(this.code, this.message);

  final String code;
  final String message;

  @override
  String toString() => 'RealtimeMicrophoneException($code, $message)';
}

/// Recorder boundary used by the Aanya call controller.
abstract interface class VoiceRecorder {
  Future<bool> hasPermission();

  Future<void> startWav(String outputPath);

  Future<String?> stop();

  Future<void> cancel();

  Future<void> dispose();
}

/// Raw microphone boundary for the persistent realtime session.
abstract interface class RealtimeVoiceRecorder {
  Future<bool> hasPermission();

  /// Warms the channel/format path without opening the microphone.
  Future<void> prepare() async {}

  Future<Stream<Uint8List>> startPcm16Stream();

  Future<void> stopStream();

  Future<void> cancel();

  Future<void> dispose();
}

/// Streaming playback boundary used by the Aanya call controller.
abstract interface class VoicePlayback {
  Future<void> play(Uri audioUri, {required void Function() onPlaybackStarted});

  Future<void> stop();

  Future<void> dispose();
}

/// Incremental mono PCM16 playback. Implementations must preserve append order.
abstract interface class RealtimeVoicePlayback {
  /// Warms the channel/format path without starting audible playback.
  Future<void> prepare({int sampleRateHz = 24000}) async {}

  Future<void> start({required int sampleRateHz});

  Future<void> append(Uint8List bytes);

  /// Waits until accepted PCM has played, then releases the active track.
  Future<void> finish();

  Future<void> stop();

  Future<void> dispose();
}

/// Owns opaque temporary recording paths and their deletion policy.
abstract interface class TemporaryRecordingStore {
  Future<String> allocateWavPath();

  Future<void> delete(String path);

  Future<void> dispose();
}
