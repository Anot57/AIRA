import 'dart:async';

import 'package:flutter/services.dart';
import 'package:record/record.dart';

import '../application/voice_io.dart';

/// Production Android AudioRecord adapter.
///
/// The record package is deliberately used only for the established runtime
/// permission flow. PCM capture itself always comes from the native channels.
final class AndroidPcmVoiceRecorder implements RealtimeVoiceRecorder {
  AndroidPcmVoiceRecorder({
    AudioRecorder? permissionRecorder,
    Future<bool> Function()? permissionChecker,
    MethodChannel? methodChannel,
    EventChannel? eventChannel,
  }) : _permissionRecorder = permissionChecker == null
           ? (permissionRecorder ?? AudioRecorder())
           : null,
       _permissionChecker = permissionChecker,
       _methodChannel =
           methodChannel ?? const MethodChannel('aira/realtime_pcm_capture'),
       _eventChannel =
           eventChannel ??
           const EventChannel('aira/realtime_pcm_capture/events');

  final AudioRecorder? _permissionRecorder;
  final Future<bool> Function()? _permissionChecker;
  final MethodChannel _methodChannel;
  final EventChannel _eventChannel;

  StreamSubscription<Object?>? _nativeSubscription;
  StreamController<Uint8List>? _turnController;
  Future<void>? _closeInFlight;
  bool _disposed = false;
  int capturedBytes = 0;
  int capturedFrames = 0;
  int deliveredBytes = 0;
  String nativeState = 'uninitialized';
  String? audioSource;

  @override
  Future<bool> hasPermission() =>
      _permissionChecker?.call() ?? _permissionRecorder!.hasPermission();

  @override
  Future<void> prepare() async {
    if (_disposed) throw StateError('Native microphone recorder is disposed.');
    final result = await _methodChannel.invokeMapMethod<String, Object?>(
      'prepare',
    );
    _updateSnapshot(result);
  }

  @override
  Future<Stream<Uint8List>> startPcm16Stream() async {
    await _closeInFlight;
    if (_disposed) throw StateError('Native microphone recorder is disposed.');
    if (_turnController != null) {
      throw StateError('A native microphone stream is already active.');
    }

    capturedBytes = 0;
    capturedFrames = 0;
    deliveredBytes = 0;
    audioSource = null;
    final controller = StreamController<Uint8List>(sync: true);
    _turnController = controller;
    _nativeSubscription = _eventChannel.receiveBroadcastStream().listen(
      _handleNativeEvent,
      onError: (Object error, StackTrace stack) {
        _failCurrent('native_event_channel_failed', error.toString(), stack);
      },
      cancelOnError: false,
    );
    try {
      final result = await _methodChannel.invokeMapMethod<String, Object?>(
        'start',
      );
      _updateSnapshot(result);
      return controller.stream;
    } on PlatformException catch (error, stack) {
      await _nativeSubscription?.cancel();
      _nativeSubscription = null;
      _turnController = null;
      await controller.close();
      Error.throwWithStackTrace(
        RealtimeMicrophoneException(
          error.code,
          error.message ?? 'Android microphone failed to start.',
        ),
        stack,
      );
    }
  }

  void _handleNativeEvent(Object? raw) {
    if (raw is! Map) {
      _failCurrent(
        'invalid_native_audio_event',
        'Android microphone returned an invalid event.',
      );
      return;
    }
    final event = raw.cast<Object?, Object?>();
    _updateSnapshot(event);
    switch (event['type']) {
      case 'pcm':
        final bytes = event['bytes'];
        if (bytes is! Uint8List || bytes.isEmpty || bytes.length.isOdd) {
          _failCurrent(
            'invalid_native_pcm',
            'Android microphone returned invalid PCM16 audio.',
          );
          return;
        }
        deliveredBytes += bytes.length;
        _turnController?.add(Uint8List.fromList(bytes));
      case 'error':
        _failCurrent(
          event['code'] as String? ?? 'native_capture_failed',
          event['message'] as String? ?? 'Android microphone capture failed.',
        );
      case 'stopped':
        unawaited(_closeCurrent());
      case 'started' || 'state':
        break;
    }
  }

  void _updateSnapshot(Map<Object?, Object?>? snapshot) {
    if (snapshot == null) return;
    nativeState = snapshot['state'] as String? ?? nativeState;
    capturedBytes = snapshot['capturedBytes'] as int? ?? capturedBytes;
    capturedFrames = snapshot['capturedFrames'] as int? ?? capturedFrames;
    audioSource = snapshot['audioSource'] as String? ?? audioSource;
  }

  void _failCurrent(String code, String message, [StackTrace? stack]) {
    final controller = _turnController;
    if (controller == null || controller.isClosed) return;
    controller.addError(
      RealtimeMicrophoneException(code, message),
      stack ?? StackTrace.current,
    );
    unawaited(_closeCurrent());
  }

  Future<void> _closeCurrent() {
    final existing = _closeInFlight;
    if (existing != null) return existing;
    final operation = _closeCurrentOnce();
    _closeInFlight = operation;
    return operation.whenComplete(() {
      if (identical(_closeInFlight, operation)) _closeInFlight = null;
    });
  }

  Future<void> _closeCurrentOnce() async {
    final controller = _turnController;
    _turnController = null;
    final subscription = _nativeSubscription;
    _nativeSubscription = null;
    await subscription?.cancel();
    if (controller != null && !controller.isClosed) await controller.close();
  }

  @override
  Future<void> stopStream() async {
    if (_turnController == null) return;
    final snapshot = await _methodChannel.invokeMapMethod<String, Object?>(
      'stop',
    );
    _updateSnapshot(snapshot);
    await _closeCurrent();
  }

  @override
  Future<void> cancel() async {
    if (_turnController != null) {
      try {
        final snapshot = await _methodChannel.invokeMapMethod<String, Object?>(
          'cancel',
        );
        _updateSnapshot(snapshot);
      } on PlatformException {
        // Native teardown is best effort after lifecycle loss.
      }
    }
    await _closeCurrent();
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    await cancel();
    _disposed = true;
    await _permissionRecorder?.dispose();
  }
}
