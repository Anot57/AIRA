import 'dart:async';

import 'package:flutter/services.dart';
import 'package:record/record.dart';

import '../application/voice_io.dart';

/// Production Android AudioRecord adapter.
///
/// The record package is deliberately used only for the established runtime
/// permission flow. PCM capture itself always comes from the native channels.
final class AndroidPcmVoiceRecorder
    implements
        RealtimeVoiceRecorder,
        CallGenerationAwareRealtimeVoiceRecorder {
  AndroidPcmVoiceRecorder({
    AudioRecorder? permissionRecorder,
    Future<bool> Function()? permissionChecker,
    MethodChannel? methodChannel,
    EventChannel? eventChannel,
    this.platformOperationTimeout = const Duration(seconds: 3),
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
  final Duration platformOperationTimeout;

  StreamSubscription<Object?>? _nativeSubscription;
  StreamController<Uint8List>? _turnController;
  Future<void>? _closeInFlight;
  bool _disposed = false;
  int? _callGeneration;
  int capturedBytes = 0;
  int capturedFrames = 0;
  int deliveredBytes = 0;
  String nativeState = 'uninitialized';
  String? audioSource;

  @override
  void bindCallGeneration(int generation) {
    if (_disposed) throw StateError('Native microphone recorder is disposed.');
    if (generation <= 0) throw ArgumentError.value(generation, 'generation');
    _callGeneration = generation;
  }

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
  Future<Stream<Uint8List>> startPcm16Stream({
    bool echoCancellation = false,
  }) async {
    await _closeInFlight;
    if (_disposed) throw StateError('Native microphone recorder is disposed.');
    if (_turnController != null) {
      throw StateError('A native microphone stream is already active.');
    }
    final callGeneration = _callGeneration;
    if (callGeneration == null) {
      throw StateError('Native microphone recorder has no call generation.');
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
        <String, Object?>{
          'callGeneration': callGeneration,
          'echoCancellation': echoCancellation,
        },
      ).timeout(platformOperationTimeout);
      _updateSnapshot(result);
      return controller.stream;
    } on Object catch (error, stack) {
      unawaited(_cancelNative(callGeneration));
      try {
        await _nativeSubscription?.cancel().timeout(platformOperationTimeout);
      } on Object {
        // A detached event channel must not hold call teardown open.
      }
      _nativeSubscription = null;
      _turnController = null;
      // Nobody has received this stream, so its close() future never
      // completes; awaiting it would turn every start failure into a hang.
      unawaited(controller.close());
      if (error is PlatformException) {
        Error.throwWithStackTrace(
          RealtimeMicrophoneException(
            error.code,
            error.message ?? 'Android microphone failed to start.',
          ),
          stack,
        );
      }
      Error.throwWithStackTrace(error, stack);
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
    try {
      await subscription?.cancel().timeout(platformOperationTimeout);
    } on Object {
      // Stream ownership is already cleared above.
    }
    // Do not await close(): for a stream the caller abandoned without
    // listening, it never completes and would wedge _closeInFlight, blocking
    // every later startPcm16Stream(). Listeners still receive onDone.
    if (controller != null && !controller.isClosed) {
      unawaited(controller.close());
    }
  }

  @override
  Future<void> stopStream() async {
    if (_turnController == null) return;
    final callGeneration = _callGeneration;
    if (callGeneration == null) return;
    final snapshot = await _methodChannel.invokeMapMethod<String, Object?>(
      'stop',
      <String, Object?>{'callGeneration': callGeneration},
    ).timeout(platformOperationTimeout);
    _updateSnapshot(snapshot);
    await _closeCurrent();
  }

  @override
  Future<void> cancel() async {
    final callGeneration = _callGeneration;
    if (_turnController != null) {
      try {
        final snapshot = callGeneration == null
            ? null
            : await _methodChannel.invokeMapMethod<String, Object?>(
                'cancel',
                <String, Object?>{'callGeneration': callGeneration},
              ).timeout(platformOperationTimeout);
        _updateSnapshot(snapshot);
      } on Object {
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

  Future<void> _cancelNative(int callGeneration) async {
    try {
      await _methodChannel.invokeMapMethod<String, Object?>(
        'cancel',
        <String, Object?>{'callGeneration': callGeneration},
      ).timeout(platformOperationTimeout);
    } on Object {
      // A timed-out start is already invalid from the Dart call's perspective.
    }
  }
}
