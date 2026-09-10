import 'dart:async';

import 'package:flutter/services.dart';

import '../application/voice_io.dart';

final class ActiveCallPlatformException implements Exception {
  const ActiveCallPlatformException(this.code, this.message);

  final String code;
  final String message;

  @override
  String toString() => 'ActiveCallPlatformException($code, $message)';
}

/// Flutter bridge to the process-owned Android microphone foreground service.
final class AndroidActiveCallService implements ActiveCallPlatform {
  AndroidActiveCallService({
    MethodChannel? methodChannel,
    EventChannel? eventChannel,
  }) : _methodChannel =
           methodChannel ?? const MethodChannel('aira/active_call'),
       _eventChannel =
           eventChannel ?? const EventChannel('aira/active_call/events') {
    _subscription = _eventChannel.receiveBroadcastStream().listen(
      _handleEvent,
      onError: _events.addError,
      cancelOnError: false,
    );
  }

  final MethodChannel _methodChannel;
  final EventChannel _eventChannel;
  final StreamController<ActiveCallPlatformEvent> _events =
      StreamController<ActiveCallPlatformEvent>.broadcast(sync: true);
  StreamSubscription<Object?>? _subscription;
  final Map<int, Completer<void>> _serviceStarts = <int, Completer<void>>{};
  bool _disposed = false;

  @override
  Stream<ActiveCallPlatformEvent> get events => _events.stream;

  @override
  Future<void> startCall({required int generation}) async {
    final ready = Completer<void>();
    _serviceStarts[generation] = ready;
    try {
      await _invoke('startCall', <String, Object?>{'generation': generation});
      await ready.future.timeout(const Duration(seconds: 5));
    } finally {
      if (identical(_serviceStarts[generation], ready)) {
        _serviceStarts.remove(generation);
      }
    }
  }

  @override
  Future<void> updateState({
    required int generation,
    required String state,
    String? sessionId,
  }) => _invoke('updateState', <String, Object?>{
    'generation': generation,
    'state': state,
    'sessionId': ?sessionId,
  });

  @override
  Future<void> endCall({required int generation}) =>
      _invoke('endCall', <String, Object?>{'generation': generation});

  Future<void> _invoke(String method, Map<String, Object?> arguments) async {
    if (_disposed) throw StateError('Active call platform is disposed.');
    try {
      await _methodChannel.invokeMapMethod<String, Object?>(method, arguments);
    } on PlatformException catch (error) {
      throw ActiveCallPlatformException(
        error.code,
        error.message ?? 'Android active-call operation failed.',
      );
    }
  }

  void _handleEvent(Object? raw) {
    if (_disposed || raw is! Map) return;
    final event = raw.cast<Object?, Object?>();
    final generation = event['generation'];
    final monotonicNanos = event['monotonicNanos'];
    final active = event['active'];
    final wireType = event['type'];
    if (generation is! int ||
        monotonicNanos is! int ||
        active is! bool ||
        wireType is! String) {
      _events.addError(
        const FormatException('Android returned an invalid active-call event.'),
      );
      return;
    }
    final type = switch (wireType) {
      'foreground_service_started' =>
        ActiveCallPlatformEventType.foregroundServiceStarted,
      'audio_focus' => ActiveCallPlatformEventType.audioFocus,
      'end_requested' => ActiveCallPlatformEventType.endRequested,
      'ended' => ActiveCallPlatformEventType.ended,
      _ => ActiveCallPlatformEventType.state,
    };
    if (type == ActiveCallPlatformEventType.foregroundServiceStarted) {
      final ready = _serviceStarts[generation];
      if (ready != null && !ready.isCompleted) ready.complete();
    }
    _events.add(
      ActiveCallPlatformEvent(
        type: type,
        generation: generation,
        monotonicNanos: monotonicNanos,
        active: active,
        state: event['state'] as String?,
        audioFocus: event['audioFocus'] as String?,
        reason: event['reason'] as String?,
      ),
    );
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    for (final pending in _serviceStarts.values) {
      if (!pending.isCompleted) {
        pending.completeError(StateError('Active call platform was disposed.'));
      }
    }
    _serviceStarts.clear();
    await _subscription?.cancel();
    await _events.close();
  }
}
