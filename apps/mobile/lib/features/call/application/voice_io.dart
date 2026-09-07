/// Recorder boundary used by the Aanya call controller.
abstract interface class VoiceRecorder {
  Future<bool> hasPermission();

  Future<void> startWav(String outputPath);

  Future<String?> stop();

  Future<void> cancel();

  Future<void> dispose();
}

/// Streaming playback boundary used by the Aanya call controller.
abstract interface class VoicePlayback {
  Future<void> play(Uri audioUri, {required void Function() onPlaybackStarted});

  Future<void> stop();

  Future<void> dispose();
}

/// Owns opaque temporary recording paths and their deletion policy.
abstract interface class TemporaryRecordingStore {
  Future<String> allocateWavPath();

  Future<void> delete(String path);

  Future<void> dispose();
}
