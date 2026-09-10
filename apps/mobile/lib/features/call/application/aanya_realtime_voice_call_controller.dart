import 'dart:async';
import 'package:flutter/foundation.dart';

import '../domain/conversation_session_turn.dart';
import '../domain/realtime_voice_protocol.dart';
import 'aanya_realtime_client.dart';
import 'aanya_voice_call_controller.dart';
import 'pcm_voice_activity_endpoint_detector.dart';
import 'voice_io.dart';

typedef MonotonicNow = Duration Function();

final Stopwatch _processMonotonicClock = Stopwatch()..start();

final class RealtimeTurnAudioAccounting {
  const RealtimeTurnAudioAccounting({
    required this.capturedBytes,
    required this.deliveredToFlutterBytes,
    required this.queuedForNetworkBytes,
    required this.sentToWebSocketBytes,
  });

  const RealtimeTurnAudioAccounting.empty()
    : capturedBytes = 0,
      deliveredToFlutterBytes = 0,
      queuedForNetworkBytes = 0,
      sentToWebSocketBytes = 0;

  final int capturedBytes;
  final int deliveredToFlutterBytes;
  final int queuedForNetworkBytes;
  final int sentToWebSocketBytes;
}

/// Rechunks arbitrary recorder buffers into bounded 40 ms PCM16 frames.
final class Pcm16FrameFramer {
  Pcm16FrameFramer({this.frameBytes = 1280}) {
    if (frameBytes <= 0 ||
        frameBytes.isOdd ||
        frameBytes > AiraRealtimeProtocol.maxFrameBytes) {
      throw ArgumentError.value(frameBytes, 'frameBytes');
    }
  }

  final int frameBytes;
  final List<int> _pending = <int>[];

  List<Uint8List> add(Uint8List bytes) {
    _pending.addAll(bytes);
    final frames = <Uint8List>[];
    while (_pending.length >= frameBytes) {
      frames.add(Uint8List.fromList(_pending.take(frameBytes).toList()));
      _pending.removeRange(0, frameBytes);
    }
    return frames;
  }

  Uint8List? flush() {
    if (_pending.isEmpty) return null;
    if (_pending.length.isOdd) {
      _pending.clear();
      throw const FormatException('Microphone ended with incomplete PCM16.');
    }
    final frame = Uint8List.fromList(_pending);
    _pending.clear();
    return frame;
  }
}

/// Long-lived, automatic Android realtime call loop.
///
/// The foreground service owns native active-call intent and device I/O. This
/// controller owns the persistent protocol session and deterministic turn
/// state, independently from transient Flutter lifecycle notifications.
final class AanyaRealtimeVoiceCallController extends ChangeNotifier
    implements AanyaVoiceCallCoordinator {
  AanyaRealtimeVoiceCallController({
    required String companionId,
    required this.client,
    required this.recorder,
    required this.playback,
    ActiveCallPlatform? activeCallPlatform,
    this.endpointConfig = const PcmVoiceActivityConfig(),
    this.zeroAudioTimeout = const Duration(milliseconds: 950),
    this.preRollDuration = const Duration(milliseconds: 400),
    this.trailingSpeechPadding = const Duration(milliseconds: 200),
    this.maxPendingNetworkBytes = 256 * 1024,
    MonotonicNow? monotonicNow,
    DateTime Function()? wallClockNow,
    Future<void> Function(Duration)? recoveryDelay,
  }) : activeCallPlatform =
           activeCallPlatform ?? const NoopActiveCallPlatform(),
       _monotonicNow = monotonicNow ?? (() => _processMonotonicClock.elapsed),
       _wallClockNow = wallClockNow ?? DateTime.now,
       _recoveryDelay = recoveryDelay ?? Future<void>.delayed {
    if (companionId != AanyaVoiceCallController.supportedCompanionId) {
      throw ArgumentError.value(companionId, 'companionId');
    }
    endpointConfig.validate();
    if (zeroAudioTimeout <= Duration.zero ||
        preRollDuration.isNegative ||
        trailingSpeechPadding.isNegative ||
        maxPendingNetworkBytes <= 0) {
      throw ArgumentError('Realtime microphone limits must be positive.');
    }
  }

  static const int _captureSampleRateHz = 16000;
  static const int _captureBytesPerSample = 2;

  final AanyaRealtimeClient client;
  final RealtimeVoiceRecorder recorder;
  final RealtimeVoicePlayback playback;
  final ActiveCallPlatform activeCallPlatform;
  final PcmVoiceActivityConfig endpointConfig;
  final Duration zeroAudioTimeout;
  final Duration preRollDuration;
  final Duration trailingSpeechPadding;
  final int maxPendingNetworkBytes;
  final MonotonicNow _monotonicNow;
  final DateTime Function() _wallClockNow;
  final Future<void> Function(Duration) _recoveryDelay;

  VoiceCallState _state = VoiceCallState.initial();
  @override
  VoiceCallState get state => _state;

  StreamSubscription<RealtimeServerEvent>? _eventSubscription;
  StreamSubscription<RealtimeAudioFrame>? _audioSubscription;
  StreamSubscription<ActiveCallPlatformEvent>? _platformSubscription;
  StreamSubscription<Uint8List>? _microphoneSubscription;
  Completer<void>? _microphoneDone;
  Pcm16FrameFramer? _framer;
  PcmVoiceActivityEndpointDetector? _endpointDetector;
  final List<Uint8List> _preRollFrames = <Uint8List>[];
  final List<Uint8List> _trailingFrames = <Uint8List>[];
  Future<void> _audioQueue = Future<void>.value();
  Future<void> _microphoneWriteQueue = Future<void>.value();
  Future<void>? _listenInFlight;
  Future<void>? _finishInFlight;
  Future<void>? _endInFlight;
  var _operationGeneration = 0;
  var _callGeneration = 0;
  var _turnNumber = 0;
  var _initialized = false;
  var _audioPlumbingReady = false;
  var _disposed = false;
  var _callActive = false;
  var _ending = false;
  var _turnInProgress = false;
  var _microphoneActive = false;
  var _finishingMicrophone = false;
  var _playbackStarted = false;
  var _captureRecoveryAttempt = 0;
  var _preRollBytes = 0;
  var _trailingBytes = 0;
  var _preSpeechBytesSent = 0;
  String? _turnId;
  String? _clientTurnId;
  String _transcript = '';
  String _response = '';
  Future<void> _shutdownComplete = Future<void>.value();
  Timer? _zeroAudioTimer;
  var _capturedBytes = 0;
  var _deliveredBytes = 0;
  var _queuedBytes = 0;
  var _sentBytes = 0;
  final Set<String> _loggedBoundaries = <String>{};

  int get _maximumPreRollBytes =>
      (_captureSampleRateHz *
          _captureBytesPerSample *
          preRollDuration.inMicroseconds) ~/
      Duration.microsecondsPerSecond;

  int get _maximumTrailingBytes =>
      (_captureSampleRateHz *
          _captureBytesPerSample *
          trailingSpeechPadding.inMicroseconds) ~/
      Duration.microsecondsPerSecond;

  RealtimeTurnAudioAccounting get audioAccounting =>
      RealtimeTurnAudioAccounting(
        capturedBytes: _capturedBytes,
        deliveredToFlutterBytes: _deliveredBytes,
        queuedForNetworkBytes: _queuedBytes,
        sentToWebSocketBytes: _sentBytes,
      );

  @override
  Future<void> get shutdownComplete => _shutdownComplete;

  @override
  Future<void> initialize() async {
    if (_initialized || _disposed) return;
    _initialized = true;
    _logTiming('call_screen_entered');
    client.addListener(_handleClientState);
    _eventSubscription = client.events.listen(_handleEvent);
    _audioSubscription = client.audioFrames.listen(_handleAudioFrame);
    _platformSubscription = activeCallPlatform.events.listen(
      _handlePlatformEvent,
      onError: (Object error, StackTrace stack) {
        _debugLog('platform_event_error=${error.runtimeType}');
      },
      cancelOnError: false,
    );

    // The transport and handshake are deliberately warmed on screen entry so
    // the first real utterance does not pay for WebSocket/session creation.
    await Future.wait(<Future<void>>[
      client.connect(),
      _prepareAudioPlumbing(speculative: true),
    ]);
  }

  Future<void> _prepareAudioPlumbing({bool speculative = false}) async {
    if (_audioPlumbingReady) return;
    try {
      await Future.wait(<Future<void>>[
        recorder.prepare(),
        playback.prepare(),
      ]);
      _audioPlumbingReady = true;
      _logTiming('audio_plumbing_ready');
    } on Object catch (error) {
      // Start Call retries both preparations after permission is available.
      // A failed speculative prewarm must not make the screen unusable.
      if (speculative) {
        _debugLog('audio_plumbing_prewarm_failed=${error.runtimeType}');
        return;
      }
      rethrow;
    }
  }

  @override
  Future<bool> startCall() async {
    if (_disposed || _callActive || _ending || !_state.canStartCall) {
      return false;
    }
    final operation = ++_operationGeneration;
    final callGeneration = ++_callGeneration;
    _callActive = true;
    _turnNumber = 0;
    _captureRecoveryAttempt = 0;
    _logTiming('call_start_pressed');
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.connecting,
        connection: client.state.isSessionReady
            ? LocalAiConnection.connected
            : LocalAiConnection.checking,
        statusLabel: 'Connecting',
        turns: const <ConversationSessionTurn>[],
        callActive: true,
        callStartedAt: _wallClockNow(),
        callGeneration: callGeneration,
        clearUserMessage: true,
      ),
    );

    try {
      if (!await recorder.hasPermission()) {
        await _abortCallStart(
          operation,
          'Microphone permission is needed for an Aanya AI call. You can '
          'allow it in Android settings and try again.',
          platformMayBeActive: false,
        );
        return false;
      }
      if (!_currentCall(operation, callGeneration)) return false;

      await Future.wait(<Future<void>>[
        activeCallPlatform.startCall(generation: callGeneration),
        client.connect(),
        _prepareAudioPlumbing(),
      ]);
      if (!_currentCall(operation, callGeneration)) return false;
      _logTiming('foreground_service_started');
      _logTiming('recorder_ready');
      _logTiming('call_started');

      if (client.state.canStartTurn) {
        await _ensureListening();
      } else {
        _setActivePhase(
          client.state.phase == AanyaRealtimePhase.recovering
              ? VoiceCallPhase.reconnecting
              : VoiceCallPhase.connecting,
          client.state.phase == AanyaRealtimePhase.recovering
              ? 'Reconnecting'
              : 'Connecting',
        );
      }
      return true;
    } on Object catch (error) {
      _debugLog('call_start_failed=${error.runtimeType}');
      await _abortCallStart(
        operation,
        "Aira couldn't start the Android AI call service. Please try again.",
        platformMayBeActive: true,
      );
      return false;
    }
  }

  Future<void> _abortCallStart(
    int operation,
    String message, {
    required bool platformMayBeActive,
  }) async {
    if (_disposed || operation != _operationGeneration) return;
    _callActive = false;
    ++_operationGeneration;
    if (platformMayBeActive) {
      try {
        await activeCallPlatform.endCall(generation: _callGeneration);
      } on Object {
        _debugLog('platform_start_rollback_failed');
      }
    }
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.error,
        connection: client.state.isSessionReady
            ? LocalAiConnection.connected
            : LocalAiConnection.offline,
        statusLabel: 'Call could not start',
        userMessage: message,
        callActive: false,
        clearCallStartedAt: true,
      ),
    );
  }

  @override
  Future<void> endCall() => _endCall(fromPlatform: false);

  Future<void> _endCall({required bool fromPlatform}) {
    final existing = _endInFlight;
    if (existing != null) return existing;
    if (_disposed || (!_callActive && !_ending)) return Future<void>.value();
    final operation = _performEndCall(fromPlatform: fromPlatform);
    _endInFlight = operation;
    return operation.whenComplete(() {
      if (identical(_endInFlight, operation)) _endInFlight = null;
    });
  }

  Future<void> _performEndCall({required bool fromPlatform}) async {
    final callGeneration = _callGeneration;
    _callActive = false;
    _ending = true;
    ++_operationGeneration;
    _logTiming('call_end_requested');
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.ending,
        statusLabel: 'Ending',
        callActive: true,
        clearUserMessage: true,
      ),
    );

    // Transport close performs bounded server-turn cancellation and sends
    // session_end. Device release starts immediately and in parallel.
    await Future.wait(<Future<void>>[
      _stopTurnResources(cancelRecorder: true),
      client.disconnect(),
      if (!fromPlatform) activeCallPlatform.endCall(generation: callGeneration),
    ]);
    if (_disposed) return;
    _ending = false;
    _resetTurnState();
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.ended,
        connection: LocalAiConnection.offline,
        statusLabel: 'Call ended',
        callActive: false,
        clearCallStartedAt: true,
        clearUserMessage: true,
      ),
    );
    _logTiming('call_ended');
  }

  @override
  Future<void> retryConnection() async {
    if (_disposed) return;
    await client.connect();
    if (_callActive && client.state.canStartTurn) await _ensureListening();
  }

  /// Compatibility entrypoint retained for tests/diagnostics. Normal UI calls
  /// [startCall] and automatic endpointing finalizes each turn.
  @override
  Future<bool> beginRecording() async {
    if (!_callActive) return startCall();
    if (_state.phase == VoiceCallPhase.listening) return true;
    if (client.state.canStartTurn) {
      await _ensureListening();
      return _state.phase == VoiceCallPhase.listening;
    }
    return false;
  }

  Future<void> _ensureListening() {
    if (_disposed ||
        !_callActive ||
        _ending ||
        _microphoneActive ||
        _turnInProgress ||
        !client.state.canStartTurn) {
      return Future<void>.value();
    }
    final existing = _listenInFlight;
    if (existing != null) return existing;
    final operation = _startListening();
    _listenInFlight = operation;
    return operation.whenComplete(() {
      if (identical(_listenInFlight, operation)) _listenInFlight = null;
    });
  }

  Future<void> _startListening() async {
    final token = ++_operationGeneration;
    final callGeneration = _callGeneration;
    _setActivePhase(VoiceCallPhase.listening, 'Listening');
    try {
      await playback.stop();
      if (!_currentCall(token, callGeneration)) return;
      final stream = await recorder.startPcm16Stream();
      if (!_currentCall(token, callGeneration)) {
        await recorder.cancel();
        return;
      }

      _microphoneActive = true;
      _finishingMicrophone = false;
      _playbackStarted = false;
      _turnInProgress = false;
      _turnId = null;
      _clientTurnId = null;
      _transcript = '';
      _response = '';
      _loggedBoundaries.clear();
      _capturedBytes = 0;
      _deliveredBytes = 0;
      _queuedBytes = 0;
      _sentBytes = 0;
      _preRollBytes = 0;
      _preRollFrames.clear();
      _trailingBytes = 0;
      _trailingFrames.clear();
      _preSpeechBytesSent = 0;
      _microphoneWriteQueue = Future<void>.value();
      _framer = Pcm16FrameFramer();
      _endpointDetector = PcmVoiceActivityEndpointDetector(
        config: endpointConfig,
      );
      final done = Completer<void>();
      _microphoneDone = done;
      _microphoneSubscription = stream.listen(
        (bytes) => _handleMicrophoneBytes(token, bytes),
        onError: (Object error, StackTrace stack) {
          if (!done.isCompleted) done.complete();
          unawaited(_recoverCapture(token, error));
        },
        onDone: () {
          if (!done.isCompleted) done.complete();
          if (_currentOperation(token) &&
              _microphoneActive &&
              !_finishingMicrophone) {
            unawaited(
              _recoverCapture(
                token,
                const RealtimeMicrophoneException(
                  'capture_stopped',
                  'Android microphone capture stopped unexpectedly.',
                ),
              ),
            );
          }
        },
        cancelOnError: true,
      );
      _zeroAudioTimer?.cancel();
      _zeroAudioTimer = Timer(zeroAudioTimeout, () {
        if (_currentOperation(token) &&
            _callActive &&
            _microphoneActive &&
            _capturedBytes == 0) {
          unawaited(
            _recoverCapture(
              token,
              const RealtimeMicrophoneException(
                'zero_audio_timeout',
                'Android produced no microphone PCM.',
              ),
            ),
          );
        }
      });
      _captureRecoveryAttempt = 0;
      _logTiming('listening_started');
      _updatePlatformState('listening');
    } on Object catch (error) {
      if (_currentCall(token, callGeneration)) {
        await _recoverCapture(token, error);
      }
    }
  }

  void _handleMicrophoneBytes(int token, Uint8List bytes) {
    if (!_currentOperation(token) || !_microphoneActive || _ending) return;
    try {
      _capturedBytes += bytes.length;
      _deliveredBytes += bytes.length;
      if (_capturedBytes > 0) {
        _zeroAudioTimer?.cancel();
        _zeroAudioTimer = null;
      }
      final frames = _framer!.add(bytes);
      final observedAt = _monotonicNow();
      final observation = _endpointDetector!.observe(bytes, observedAt);
      if (!_turnInProgress) {
        for (final frame in frames) {
          _addPreRoll(frame);
        }
      } else if (observation.state == PcmVoiceActivityState.speechActive) {
        _flushTrailingFrames(token);
        for (final frame in frames) {
          _queueMicrophoneFrame(token, frame);
        }
      } else {
        // Hold only a small amount of post-voice PCM. The full 3-second VAD
        // wait remains local, but batch STT no longer receives seconds of
        // known silence after every utterance.
        for (final frame in frames) {
          _addTrailingFrame(frame);
        }
      }
      if (observation.speechStarted && !_turnInProgress) {
        if (!client.beginTurn()) {
          unawaited(
            _recoverCapture(
              token,
              StateError('Realtime session stopped accepting a new turn.'),
            ),
          );
          return;
        }
        _turnInProgress = true;
        _turnNumber += 1;
        _clientTurnId = 'client_${_callGeneration}_$_turnNumber';
        _preSpeechBytesSent = _preRollBytes;
        _logTiming(
          'speech_detected',
          extra:
              'rms=${observation.rms.toStringAsFixed(2)} '
              'pre_speech_pcm_bytes=$_preSpeechBytesSent '
              'speech_start_timestamp=${observedAt.inMicroseconds}',
        );
        for (final frame in _preRollFrames) {
          _queueMicrophoneFrame(token, frame);
        }
        _preRollFrames.clear();
        _preRollBytes = 0;
      }
      if (observation.endpointReached && _turnInProgress) {
        final lastSpeechUs = observation.lastSpeech?.inMicroseconds ?? -1;
        final speechOnsetUs = observation.speechOnset?.inMicroseconds ?? -1;
        final endpointElapsedMs = speechOnsetUs >= 0
            ? (observedAt.inMicroseconds - speechOnsetUs) ~/ 1000
            : -1;
        _logTiming(
          'last_voice_detected',
          monotonicAt: observation.lastSpeech,
          extra: 'speech_timestamp_us=$lastSpeechUs',
        );
        _logTiming(
          'silence_endpoint_fired',
          extra:
              'threshold_ms=${endpointConfig.silenceEndpoint.inMilliseconds} '
              'endpoint_timestamp=${observedAt.inMicroseconds} '
              'endpoint_elapsed_since_onset_ms=$endpointElapsedMs',
        );
        unawaited(_finalizeUserTurn(token));
      }
    } on Object catch (error) {
      unawaited(_recoverCapture(token, error));
    }
  }

  void _addPreRoll(Uint8List frame) {
    final maximum = _maximumPreRollBytes;
    if (maximum <= 0) return;
    final copy = Uint8List.fromList(frame);
    _preRollFrames.add(copy);
    _preRollBytes += copy.length;
    while (_preRollBytes > maximum && _preRollFrames.isNotEmpty) {
      _preRollBytes -= _preRollFrames.removeAt(0).length;
    }
  }

  void _addTrailingFrame(Uint8List frame) {
    final maximum = _maximumTrailingBytes;
    if (maximum <= 0 || _trailingBytes + frame.length > maximum) return;
    final copy = Uint8List.fromList(frame);
    _trailingFrames.add(copy);
    _trailingBytes += copy.length;
  }

  void _flushTrailingFrames(int token) {
    for (final frame in _trailingFrames) {
      _queueMicrophoneFrame(token, frame);
    }
    _trailingFrames.clear();
    _trailingBytes = 0;
  }

  void _queueMicrophoneFrame(int token, Uint8List frame) {
    if (_queuedBytes + frame.length > maxPendingNetworkBytes) {
      unawaited(
        _recoverCapture(
          token,
          StateError('The microphone network queue reached its safe bound.'),
        ),
      );
      return;
    }
    _queuedBytes += frame.length;
    _microphoneWriteQueue = _microphoneWriteQueue.then((_) async {
      if (!_currentOperation(token) || !_turnInProgress) return;
      final sent = await client.sendPcm16ChunkOrdered(frame);
      if (!_currentOperation(token) || !_turnInProgress) return;
      _queuedBytes -= frame.length;
      if (!sent) {
        unawaited(
          _recoverCapture(
            token,
            StateError('The microphone audio could not be sent.'),
          ),
        );
        return;
      }
      _sentBytes += frame.length;
    });
  }

  @override
  Future<void> finishRecording() {
    if (!_turnInProgress) return Future<void>.value();
    return _finalizeUserTurn(_operationGeneration);
  }

  Future<void> _finalizeUserTurn(int token) {
    final existing = _finishInFlight;
    if (existing != null) return existing;
    if (!_currentOperation(token) || !_turnInProgress || _finishingMicrophone) {
      return Future<void>.value();
    }
    final operation = _finishUserTurnOnce(token);
    _finishInFlight = operation;
    return operation.whenComplete(() {
      if (identical(_finishInFlight, operation)) _finishInFlight = null;
    });
  }

  Future<void> _finishUserTurnOnce(int token) async {
    _finishingMicrophone = true;
    _zeroAudioTimer?.cancel();
    _zeroAudioTimer = null;
    _setActivePhase(VoiceCallPhase.finalizingUserTurn, 'Thinking');
    _updatePlatformState('finalizing_user_turn');
    try {
      await recorder.stopStream();
      await _microphoneDone?.future.timeout(const Duration(seconds: 2));
      _microphoneActive = false;
      await _microphoneSubscription?.cancel();
      _microphoneSubscription = null;
      final finalFrame = _framer?.flush();
      if (finalFrame != null) _addTrailingFrame(finalFrame);
      _flushTrailingFrames(token);
      await _microphoneWriteQueue;
      if (!_currentOperation(token) || !_turnInProgress) return;
      _logTiming(
        'final_pcm_delivered',
        extra:
            'captured_pcm_bytes=$_capturedBytes '
            'pre_speech_pcm_bytes=$_preSpeechBytesSent '
            'audio_sent_bytes=$_sentBytes '
            'utterance_duration_ms='
            '${(_sentBytes * 1000) ~/ (_captureSampleRateHz * _captureBytesPerSample)}',
      );
      if (_sentBytes == 0) {
        await _cancelCurrentTurnAndResume(reason: 'endpoint_without_audio');
        return;
      }
      _logTiming('end_of_turn_send_start');
      if (!await client.endTurnOrdered()) {
        await _suspendForReconnect('end_of_turn_send_failed');
        return;
      }
      _logTiming('end_of_turn_send_complete');
      _debugLog(
        'turn_audio captured_bytes=$_capturedBytes '
        'delivered_bytes=$_deliveredBytes queued_bytes=$_queuedBytes '
        'sent_bytes=$_sentBytes',
      );
    } on Object catch (error) {
      await _suspendForReconnect('microphone_finish_${error.runtimeType}');
    }
  }

  @override
  Future<void> cancelRecording() => cancelActiveTurn();

  /// Bounded internal turn cancellation retained for recovery and teardown.
  /// It is intentionally not exposed by the normal call UI.
  @override
  Future<void> cancelActiveTurn() =>
      _cancelCurrentTurnAndResume(reason: 'internal_cancel');

  Future<void> _cancelCurrentTurnAndResume({required String reason}) async {
    if (_disposed || !_callActive || _ending) return;
    final hadServerTurn = _turnInProgress;
    ++_operationGeneration;
    final serverCancellation = hadServerTurn
        ? client.cancelTurn()
        : Future<bool>.value(false);
    await _stopTurnResources(cancelRecorder: true);
    await serverCancellation;
    _resetTurnState();
    _debugLog('turn_cancelled reason=$reason');
    if (_callActive && client.state.canStartTurn) await _ensureListening();
  }

  Future<void> _recoverCapture(int token, Object error) async {
    if (!_currentOperation(token) || !_callActive || _ending) return;
    final delayIndex = _captureRecoveryAttempt.clamp(0, 4);
    const delays = <Duration>[
      Duration(milliseconds: 250),
      Duration(milliseconds: 500),
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 4),
    ];
    _captureRecoveryAttempt += 1;
    _debugLog(
      'capture_recovery attempt=$_captureRecoveryAttempt '
      'error=${error.runtimeType}',
    );
    final hadServerTurn = _turnInProgress;
    ++_operationGeneration;
    final cancellation = hadServerTurn
        ? client.cancelTurn()
        : Future<bool>.value(false);
    await _stopTurnResources(cancelRecorder: true);
    await cancellation;
    _resetTurnState();
    if (!_callActive || _ending || _disposed) return;
    _setActivePhase(VoiceCallPhase.reconnecting, 'Reconnecting');
    await _recoveryDelay(delays[delayIndex]);
    if (_callActive && !_ending && client.state.canStartTurn) {
      await _ensureListening();
    } else if (_callActive) {
      await client.connect();
    }
  }

  void _handleClientState() {
    if (_disposed) return;
    final realtime = client.state;
    if (!_callActive) {
      switch (realtime.phase) {
        case AanyaRealtimePhase.ready:
          _emit(
            _state.copyWith(
              phase: VoiceCallPhase.idle,
              connection: LocalAiConnection.connected,
              statusLabel: 'Ready to start',
              clearUserMessage: true,
            ),
          );
        case AanyaRealtimePhase.connecting ||
            AanyaRealtimePhase.aiStarting ||
            AanyaRealtimePhase.recovering:
          _emit(
            _state.copyWith(
              connection: LocalAiConnection.checking,
              statusLabel: 'Preparing call',
            ),
          );
        case AanyaRealtimePhase.offline:
          _emit(
            _state.copyWith(
              connection: LocalAiConnection.offline,
              statusLabel: 'Local AI offline',
            ),
          );
        case AanyaRealtimePhase.error:
          _emit(
            _state.copyWith(
              phase: VoiceCallPhase.error,
              connection: LocalAiConnection.offline,
              statusLabel: 'Realtime connection failed',
              userMessage: realtime.statusLabel,
            ),
          );
        case AanyaRealtimePhase.listening ||
            AanyaRealtimePhase.finalizing ||
            AanyaRealtimePhase.cancelling ||
            AanyaRealtimePhase.thinking ||
            AanyaRealtimePhase.speaking ||
            AanyaRealtimePhase.disposed:
          break;
      }
      return;
    }

    switch (realtime.phase) {
      case AanyaRealtimePhase.connecting || AanyaRealtimePhase.aiStarting:
        if (!_turnInProgress && !_microphoneActive) {
          final reconnecting =
              _state.phase == VoiceCallPhase.reconnecting ||
              realtime.statusLabel == 'Recovering';
          _setActivePhase(
            reconnecting
                ? VoiceCallPhase.reconnecting
                : VoiceCallPhase.connecting,
            reconnecting ? 'Reconnecting' : 'Connecting',
          );
        }
      case AanyaRealtimePhase.recovering || AanyaRealtimePhase.offline:
        _setActivePhase(VoiceCallPhase.reconnecting, 'Reconnecting');
        _updatePlatformState('reconnecting');
        unawaited(_suspendForReconnect('transport_reconnecting'));
      case AanyaRealtimePhase.ready:
        _updatePlatformState('listening');
        if (!_turnInProgress && !_microphoneActive && !_ending) {
          unawaited(_ensureListening());
        }
      case AanyaRealtimePhase.listening:
        if (_microphoneActive) {
          _setActivePhase(VoiceCallPhase.listening, 'Listening');
        }
      case AanyaRealtimePhase.finalizing:
        _setActivePhase(VoiceCallPhase.finalizingUserTurn, 'Thinking');
      case AanyaRealtimePhase.thinking:
        _setActivePhase(VoiceCallPhase.thinking, 'Thinking');
        _updatePlatformState('thinking');
      case AanyaRealtimePhase.speaking:
        // Actual AudioTrack start below is authoritative for visible speaking.
        break;
      case AanyaRealtimePhase.error:
        if (realtime.errorIsRecoverable) {
          unawaited(_recoverFromServerTurnError());
        } else {
          unawaited(_endAfterFatal(realtime.statusLabel));
        }
      case AanyaRealtimePhase.cancelling:
        break;
      case AanyaRealtimePhase.disposed:
        unawaited(_endAfterFatal('Realtime session was disposed.'));
    }
  }

  Future<void> _suspendForReconnect(String reason) async {
    if (_disposed || !_callActive || _ending) return;
    if (!_microphoneActive && !_turnInProgress && !_playbackStarted) return;
    ++_operationGeneration;
    await _stopTurnResources(cancelRecorder: true);
    _resetTurnState();
    _debugLog('session_suspended reason=$reason');
  }

  Future<void> _recoverFromServerTurnError() async {
    if (_disposed || !_callActive || _ending) return;
    ++_operationGeneration;
    await _stopTurnResources(cancelRecorder: true);
    _resetTurnState();
    client.clearRecoverableError();
    if (client.state.canStartTurn) await _ensureListening();
  }

  Future<void> _endAfterFatal(String message) async {
    if (_disposed || !_callActive) return;
    await _endCall(fromPlatform: false);
    if (_disposed) return;
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.error,
        statusLabel: 'Call ended',
        userMessage: message,
        callActive: false,
      ),
    );
  }

  void _handleEvent(RealtimeServerEvent event) {
    if (_disposed || !_callActive) return;
    if (event case RealtimeTurnEvent(:final turnId)) {
      if (_turnId == null) {
        _turnId = turnId;
        _logTiming('server_turn_accepted');
      }
    }
    switch (event) {
      case RealtimeSttFinalEvent(:final text):
        _transcript = text;
        _logTiming('first_text_received', once: true);
        _setActivePhase(VoiceCallPhase.thinking, 'Thinking');
      case RealtimeTextDeltaEvent():
        _response = client.state.responseText;
        _logTiming('first_text_received', once: true);
      case RealtimeTextSentenceEvent(:final text):
        if (_response.isEmpty) _response = text;
        _logTiming('first_text_received', once: true);
      case RealtimeAudioChunkEvent():
        _logTiming('first_audio_metadata_received', once: true);
      case RealtimeTurnCompleteEvent():
        unawaited(_completeTurn());
      case RealtimeErrorEvent():
        // Client-state handling owns recovery/fatal policy.
        break;
      case RealtimeSessionReadyEvent():
        _logTiming('session_ready');
      case RealtimeSttPartialEvent() ||
          RealtimeThinkingEvent() ||
          RealtimeSpeakingEvent():
        break;
    }
  }

  void _handleAudioFrame(RealtimeAudioFrame frame) {
    if (_disposed || !_callActive || !_turnInProgress || _ending) return;
    final token = _operationGeneration;
    _logTiming('first_audio_bytes_received', once: true);
    _audioQueue = _audioQueue
        .then((_) async {
          if (!_currentOperation(token) || !_turnInProgress) return;
          if (!_playbackStarted) {
            _logTiming('playback_prepare_start', once: true);
            await playback.start(
              sampleRateHz: frame.header.audioFormat.sampleRateHz,
            );
            _logTiming('playback_prepare_complete', once: true);
          }
          await playback.append(frame.bytes);
          if (!_playbackStarted) {
            _logTiming('first_audio_write', once: true);
            _playbackStarted = true;
            _setActivePhase(VoiceCallPhase.speaking, 'Speaking');
            _updatePlatformState('speaking');
            _logTiming('playback_started', once: true);
          }
        })
        .catchError((Object error) {
          if (_currentOperation(token)) {
            unawaited(_recoverFromPlaybackFailure(error));
          }
        });
  }

  Future<void> _recoverFromPlaybackFailure(Object error) async {
    _debugLog('playback_failure=${error.runtimeType}');
    await _cancelCurrentTurnAndResume(reason: 'playback_failure');
  }

  Future<void> _completeTurn() async {
    final token = _operationGeneration;
    await _audioQueue;
    if (!_currentOperation(token) || !_turnInProgress || !_callActive) return;
    if (_playbackStarted) {
      await playback.finish();
      _logTiming('playback_complete');
    }
    if (!_currentOperation(token) || !_callActive || _ending) return;
    final id = _turnId;
    if (id != null && _transcript.isNotEmpty && _response.isNotEmpty) {
      _emit(
        _state.copyWith(
          turns: <ConversationSessionTurn>[
            ..._state.turns,
            ConversationSessionTurn(
              turnId: id,
              userTranscript: _transcript,
              assistantResponse: _response,
              aiDisclosure:
                  client.state.aiDisclosure ?? 'Aanya is an AI companion.',
              audioUri: Uri(),
            ),
          ],
        ),
      );
    }
    _resetTurnState();
    // AudioTrack drain above is authoritative. Only now may capture restart,
    // preventing Aanya's own speaker output becoming the next user turn.
    await _ensureListening();
  }

  void _handlePlatformEvent(ActiveCallPlatformEvent event) {
    if (_disposed || event.generation != _callGeneration) return;
    switch (event.type) {
      case ActiveCallPlatformEventType.foregroundServiceStarted:
        _logTiming('foreground_service_started', once: true);
      case ActiveCallPlatformEventType.audioFocus:
        _logTiming('audio_focus_${event.audioFocus ?? 'unknown'}', once: false);
      case ActiveCallPlatformEventType.endRequested:
        if (_callActive) unawaited(_endCall(fromPlatform: true));
      case ActiveCallPlatformEventType.ended:
        if (_callActive) unawaited(_endCall(fromPlatform: true));
      case ActiveCallPlatformEventType.state:
        break;
    }
  }

  Future<void> _stopTurnResources({required bool cancelRecorder}) async {
    _zeroAudioTimer?.cancel();
    _zeroAudioTimer = null;
    _microphoneActive = false;
    _finishingMicrophone = true;
    await Future.wait(<Future<void>>[
      if (cancelRecorder) _safeRecorderCancel(),
      _safePlaybackStop(),
    ]);
    await _microphoneSubscription?.cancel();
    _microphoneSubscription = null;
  }

  void _resetTurnState() {
    _turnInProgress = false;
    _microphoneActive = false;
    _finishingMicrophone = false;
    _playbackStarted = false;
    _turnId = null;
    _clientTurnId = null;
    _transcript = '';
    _response = '';
    _preRollFrames.clear();
    _preRollBytes = 0;
    _trailingFrames.clear();
    _trailingBytes = 0;
    _preSpeechBytesSent = 0;
    _framer = null;
    _endpointDetector = null;
    _audioQueue = Future<void>.value();
    _finishInFlight = null;
  }

  Future<void> _safeRecorderCancel() async {
    try {
      await recorder.cancel();
    } on Object {
      _debugLog('recorder_cancel_failed');
    }
  }

  Future<void> _safePlaybackStop() async {
    try {
      await playback.stop();
    } on Object {
      _debugLog('playback_stop_failed');
    }
  }

  @override
  Future<void> handleAppBackgrounded() async {
    if (_disposed) return;
    // Home, shade, lock, screen-off, and app switching are UI state only. The
    // foreground service and persistent socket remain active.
    _logTiming('flutter_backgrounded');
    await client.setForeground(false);
  }

  @override
  void handleAppResumed() {
    if (_disposed) return;
    _logTiming('flutter_resumed');
    unawaited(client.setForeground(true));
  }

  @override
  void clearRecoverableError() {
    if (_disposed) return;
    if (_callActive) {
      client.clearRecoverableError();
      if (client.state.canStartTurn) unawaited(_ensureListening());
      return;
    }
    client.clearRecoverableError();
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.idle,
        statusLabel: client.state.isSessionReady
            ? 'Ready to start'
            : 'Preparing call',
        clearUserMessage: true,
      ),
    );
  }

  void _setActivePhase(VoiceCallPhase phase, String label) {
    if (_disposed || !_callActive || _ending) return;
    _emit(
      _state.copyWith(
        phase: phase,
        connection: phase == VoiceCallPhase.reconnecting
            ? LocalAiConnection.checking
            : LocalAiConnection.connected,
        statusLabel: label,
        callActive: true,
        clearUserMessage: true,
      ),
    );
  }

  void _updatePlatformState(String state) {
    if (_disposed || !_callActive || _ending) return;
    unawaited(
      activeCallPlatform
          .updateState(
            generation: _callGeneration,
            state: state,
            sessionId: client.state.sessionId,
          )
          .catchError((Object error) {
            _debugLog('platform_state_update_failed=${error.runtimeType}');
          }),
    );
  }

  bool _currentOperation(int token) =>
      !_disposed && token == _operationGeneration;

  bool _currentCall(int operation, int callGeneration) =>
      _currentOperation(operation) &&
      _callActive &&
      !_ending &&
      callGeneration == _callGeneration;

  void _logTiming(
    String event, {
    bool once = false,
    Duration? monotonicAt,
    String? extra,
  }) {
    if (once && !_loggedBoundaries.add(event)) return;
    final fields = <String>[
      'event=$event',
      'session_id=${client.state.sessionId ?? 'none'}',
      'turn_id=${_turnId ?? _clientTurnId ?? 'none'}',
      'client_turn_id=${_clientTurnId ?? 'none'}',
      'generation=$_callGeneration',
      'turn_number=$_turnNumber',
      'monotonic_us=${(monotonicAt ?? _monotonicNow()).inMicroseconds}',
      ?extra,
    ];
    _debugLog(fields.join(' '));
  }

  void _debugLog(String message) {
    if (kDebugMode) debugPrint('[AIRA CALL TIMING] $message');
  }

  void _emit(VoiceCallState next) {
    if (_disposed) return;
    _state = next;
    notifyListeners();
  }

  @override
  void dispose() {
    if (_disposed) return;
    final wasActive = _callActive || _ending;
    final callGeneration = _callGeneration;
    _callActive = false;
    _ending = false;
    ++_operationGeneration;
    client.removeListener(_handleClientState);
    _state = _state.copyWith(
      phase: VoiceCallPhase.disposed,
      statusLabel: 'Voice session closed',
      callActive: false,
      clearCallStartedAt: true,
      clearUserMessage: true,
    );
    notifyListeners();
    _disposed = true;
    _shutdownComplete = _shutdown(wasActive, callGeneration);
    super.dispose();
  }

  Future<void> _shutdown(bool wasActive, int callGeneration) async {
    _zeroAudioTimer?.cancel();
    await Future.wait(<Future<void>>[
      _safeRecorderCancel(),
      _safePlaybackStop(),
      _endPlatformForShutdown(wasActive, callGeneration),
    ]);
    await _microphoneSubscription?.cancel();
    await _eventSubscription?.cancel();
    await _audioSubscription?.cancel();
    await _platformSubscription?.cancel();
    client.dispose();
    await client.shutdownComplete;
    await recorder.dispose();
    await playback.dispose();
    await activeCallPlatform.dispose();
  }

  Future<void> _endPlatformForShutdown(
    bool wasActive,
    int callGeneration,
  ) async {
    if (!wasActive) return;
    try {
      await activeCallPlatform.endCall(generation: callGeneration);
    } on Object {
      _debugLog('platform_shutdown_failed');
    }
  }
}
