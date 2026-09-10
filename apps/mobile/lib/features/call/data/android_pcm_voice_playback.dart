import 'package:flutter/services.dart';

import '../application/voice_io.dart';

/// Android AudioTrack bridge for continuous mono PCM16 playback.
final class AndroidPcmVoicePlayback implements RealtimeVoicePlayback {
  AndroidPcmVoicePlayback({MethodChannel? channel})
    : _channel = channel ?? const MethodChannel(_channelName);

  static const String _channelName = 'aira/realtime_pcm_playback';
  final MethodChannel _channel;
  bool _active = false;
  bool _disposed = false;

  @override
  Future<void> prepare({int sampleRateHz = 24000}) async {
    if (_disposed) throw StateError('PCM playback is disposed.');
    await _channel.invokeMethod<void>('prepare', <String, Object?>{
      'sampleRateHz': sampleRateHz,
    });
  }

  @override
  Future<void> start({required int sampleRateHz}) async {
    if (_disposed) throw StateError('PCM playback is disposed.');
    if (_active) throw StateError('PCM playback is already active.');
    await _channel.invokeMethod<void>('start', <String, Object?>{
      'sampleRateHz': sampleRateHz,
      'channels': 1,
      'encoding': 'pcm_s16le',
    });
    _active = true;
  }

  @override
  Future<void> append(Uint8List bytes) async {
    if (!_active || _disposed || bytes.isEmpty || bytes.length.isOdd) {
      throw StateError('PCM playback received an invalid audio frame.');
    }
    await _channel.invokeMethod<void>('append', bytes);
  }

  @override
  Future<void> finish() async {
    if (!_active || _disposed) return;
    _active = false;
    await _channel.invokeMethod<void>('finish');
  }

  @override
  Future<void> stop() async {
    if (_disposed) return;
    _active = false;
    await _channel.invokeMethod<void>('stop');
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    await stop();
    _disposed = true;
  }
}
