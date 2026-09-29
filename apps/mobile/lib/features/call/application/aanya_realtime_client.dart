import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';

import '../../../core/config/aira_api_config.dart';
import '../data/realtime_web_socket.dart';
import '../domain/realtime_voice_protocol.dart';

enum AanyaRealtimePhase {
  offline,
  connecting,
  aiStarting,
  ready,
  listening,
  finalizing,
  cancelling,
  thinking,
  speaking,
  recovering,
  error,
  disposed,
}

final class AanyaRealtimeState {
  const AanyaRealtimeState({
    required this.phase,
    required this.statusLabel,
    this.sessionId,
    this.aiDisclosure,
    this.partialTranscript = '',
    this.finalTranscript = '',
    this.responseText = '',
    this.errorCode,
    this.errorIsRecoverable = false,
  });

  const AanyaRealtimeState.offline()
    : this(phase: AanyaRealtimePhase.offline, statusLabel: 'Offline');

  final AanyaRealtimePhase phase;
  final String statusLabel;
  final String? sessionId;
  final String? aiDisclosure;
  final String partialTranscript;
  final String finalTranscript;
  final String responseText;
  final String? errorCode;
  final bool errorIsRecoverable;

  bool get isSessionReady =>
      sessionId != null &&
      (phase == AanyaRealtimePhase.ready ||
          phase == AanyaRealtimePhase.listening ||
          phase == AanyaRealtimePhase.finalizing ||
          phase == AanyaRealtimePhase.cancelling ||
          phase == AanyaRealtimePhase.thinking ||
          phase == AanyaRealtimePhase.speaking ||
          (phase == AanyaRealtimePhase.error && errorIsRecoverable));

  bool get canStartTurn => phase == AanyaRealtimePhase.ready;

  AanyaRealtimeState copyWith({
    AanyaRealtimePhase? phase,
    String? statusLabel,
    String? sessionId,
    String? aiDisclosure,
    String? partialTranscript,
    String? finalTranscript,
    String? responseText,
    String? errorCode,
    bool? errorIsRecoverable,
    bool clearSession = false,
    bool clearError = false,
  }) {
    return AanyaRealtimeState(
      phase: phase ?? this.phase,
      statusLabel: statusLabel ?? this.statusLabel,
      sessionId: clearSession ? null : (sessionId ?? this.sessionId),
      aiDisclosure: clearSession ? null : (aiDisclosure ?? this.aiDisclosure),
      partialTranscript: partialTranscript ?? this.partialTranscript,
      finalTranscript: finalTranscript ?? this.finalTranscript,
      responseText: responseText ?? this.responseText,
      errorCode: clearError ? null : (errorCode ?? this.errorCode),
      errorIsRecoverable: clearError
          ? false
          : (errorIsRecoverable ?? this.errorIsRecoverable),
    );
  }
}

final class RealtimeAudioFrame {
  RealtimeAudioFrame({required this.header, required Uint8List bytes})
    : bytes = Uint8List.fromList(bytes);

  final RealtimeAudioChunkEvent header;
  final Uint8List bytes;
}

typedef ReconnectDelay = Future<void> Function(Duration duration);

final class RealtimeProtocolViolation {
  const RealtimeProtocolViolation({
    required this.code,
    required this.expectedEvent,
    required this.receivedEvent,
    required this.expectedSequence,
    required this.receivedSequence,
    required this.sessionId,
    required this.turnId,
    required this.textSequence,
    required this.audioSequence,
  });

  final String code;
  final String? expectedEvent;
  final String? receivedEvent;
  final int? expectedSequence;
  final int? receivedSequence;
  final String? sessionId;
  final String? turnId;
  final int textSequence;
  final int audioSequence;
}

/// Owns one Aanya-only realtime WebSocket session.
///
/// Whole-WAV HTTP remains implemented by [AanyaVoiceCallController]. This
/// client is the bounded, cancellation-aware foundation for incremental PCM
/// capture and playback once those adapters are wired to the UI.
final class AanyaRealtimeClient extends ChangeNotifier {
  AanyaRealtimeClient({
    required String companionId,
    required AiraApiConfig config,
    RealtimeWebSocketConnector? connector,
    this.handshakeTimeout = const Duration(seconds: 10),
    this.closeTimeout = const Duration(seconds: 2),
    this.cancelAckTimeout = const Duration(milliseconds: 400),
    this.maxTurnAudioBytes = AiraRealtimeProtocol.maxTurnAudioBytes,
    List<Duration> reconnectDelays = const <Duration>[
      Duration(milliseconds: 500),
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 4),
    ],
    ReconnectDelay? reconnectDelay,
    this.repeatLastReconnectDelay = true,
  }) : _uri = config.realtimeUri,
       _connector = connector ?? const IoRealtimeWebSocketConnector(),
       _reconnectDelay = reconnectDelay ?? _defaultReconnectDelay,
       reconnectDelays = List<Duration>.unmodifiable(reconnectDelays) {
    if (companionId != AiraRealtimeProtocol.companionId) {
      throw ArgumentError.value(
        companionId,
        'companionId',
        'Realtime voice is available only for aanya.',
      );
    }
    if (handshakeTimeout <= Duration.zero ||
        closeTimeout <= Duration.zero ||
        cancelAckTimeout <= Duration.zero ||
        maxTurnAudioBytes <= 0 ||
        maxTurnAudioBytes > AiraRealtimeProtocol.maxTurnAudioBytes ||
        reconnectDelays.any((delay) => delay.isNegative)) {
      throw ArgumentError('Realtime limits must be positive.');
    }
  }

  final Uri _uri;
  final RealtimeWebSocketConnector _connector;
  final ReconnectDelay _reconnectDelay;
  final Duration handshakeTimeout;
  final Duration closeTimeout;
  final Duration cancelAckTimeout;
  final int maxTurnAudioBytes;
  final List<Duration> reconnectDelays;
  final bool repeatLastReconnectDelay;

  final StreamController<RealtimeServerEvent> _eventController =
      StreamController<RealtimeServerEvent>.broadcast(sync: true);
  final StreamController<RealtimeAudioFrame> _audioController =
      StreamController<RealtimeAudioFrame>.broadcast(sync: true);

  AanyaRealtimeState _state = const AanyaRealtimeState.offline();
  AanyaRealtimeState get state => _state;

  Stream<RealtimeServerEvent> get events => _eventController.stream;
  Stream<RealtimeAudioFrame> get audioFrames => _audioController.stream;
  RealtimeProtocolViolation? get lastProtocolViolation =>
      _lastProtocolViolation;

  RealtimeWebSocket? _socket;
  StreamSubscription<Object?>? _socketSubscription;
  Future<void>? _connectInFlight;
  Future<void>? _reconnectInFlight;
  Completer<bool>? _cancelAcknowledgement;
  Timer? _handshakeTimer;
  RealtimeAudioChunkEvent? _pendingServerAudio;
  var _connectionGeneration = 0;
  var _reconnectScheduleGeneration = 0;
  var _reconnectAttempt = 0;
  var _nextServerAudioSequence = 0;
  var _nextServerSentenceSequence = 1;
  int? _playbackSampleRateHz;
  var _turnAudioBytes = 0;
  var _queuedTurnAudioBytes = 0;
  Future<void> _writeTail = Future<void>.value();
  var _writeGeneration = 0;
  var _serverAudioBytes = 0;
  var _turnActive = false;
  String? _activeServerTurnId;
  String? _cancellingServerTurnId;
  RealtimeProtocolViolation? _lastProtocolViolation;
  var _allowReconnect = false;
  var _intentionalClose = false;
  var _isForeground = true;
  var _disposed = false;
  Future<void> _shutdownComplete = Future<void>.value();
  final Stopwatch _monotonicClock = Stopwatch()..start();

  Future<void> get shutdownComplete => _shutdownComplete;

  /// Opens at most one socket. Reaching [AanyaRealtimePhase.aiStarting] means
  /// the handshake was sent; only session_ready transitions the client to
  /// [AanyaRealtimePhase.ready].
  Future<void> connect() {
    if (_disposed) return Future<void>.value();
    _intentionalClose = false;
    _allowReconnect = true;
    if (_socket != null) return Future<void>.value();
    final existing = _connectInFlight;
    if (existing != null) return existing;

    final generation = ++_connectionGeneration;
    final operation = _connectOnce(generation);
    _connectInFlight = operation;
    unawaited(
      operation.whenComplete(() {
        if (identical(_connectInFlight, operation)) {
          _connectInFlight = null;
        }
        if (!_disposed && _allowReconnect && _socket == null) {
          _scheduleReconnect();
        }
      }),
    );
    return operation;
  }

  Future<void> _connectOnce(int generation) async {
    _timing('websocket_connect_start', generation);
    _lastProtocolViolation = null;
    _emit(
      _state.copyWith(
        phase: _reconnectAttempt == 0
            ? AanyaRealtimePhase.connecting
            : AanyaRealtimePhase.recovering,
        statusLabel: _reconnectAttempt == 0 ? 'Connecting' : 'Recovering',
        clearSession: true,
        partialTranscript: '',
        finalTranscript: '',
        responseText: '',
        clearError: true,
      ),
    );

    RealtimeWebSocket socket;
    try {
      socket = await _connector.connect(_uri);
    } on Object catch (error) {
      if (_isConnectionCurrent(generation)) {
        _debugLog('connect failed: ${error.runtimeType}');
        _emitOffline('Offline');
        _scheduleReconnect();
      }
      return;
    }

    if (!_isConnectionCurrent(generation) || !_allowReconnect) {
      await _safeClose(socket);
      return;
    }

    _socket = socket;
    _timing('websocket_open', generation);
    _socketSubscription = socket.messages.listen(
      (message) => _handleSocketMessage(socket, generation, message),
      onError: (Object error, StackTrace stackTrace) {
        _debugLog('socket error: ${error.runtimeType}');
        _handleUnexpectedDisconnect(socket, generation);
      },
      onDone: () => _handleUnexpectedDisconnect(socket, generation),
      cancelOnError: false,
    );

    try {
      _sendControl(const RealtimeSessionStartMessage());
    } on Object catch (error) {
      _debugLog('handshake send failed: ${error.runtimeType}');
      await _handleUnexpectedDisconnect(socket, generation);
      return;
    }

    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.aiStarting,
        statusLabel: 'AI starting',
        clearError: true,
      ),
    );
    _handshakeTimer?.cancel();
    _handshakeTimer = Timer(handshakeTimeout, () {
      if (_socket == socket && _isConnectionCurrent(generation)) {
        _debugLog('session_ready timed out');
        unawaited(_handleUnexpectedDisconnect(socket, generation));
      }
    });
  }

  /// Marks the beginning of microphone capture without sending a synthetic
  /// wire message. The first and subsequent PCM chunks are binary frames.
  bool beginTurn() {
    if (_disposed || !_state.canStartTurn || _turnActive || _socket == null) {
      return false;
    }
    _turnActive = true;
    _turnAudioBytes = 0;
    _activeServerTurnId = null;
    _pendingServerAudio = null;
    _nextServerAudioSequence = 0;
    _nextServerSentenceSequence = 1;
    _serverAudioBytes = 0;
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.listening,
        statusLabel: 'Listening',
        partialTranscript: '',
        finalTranscript: '',
        responseText: '',
        clearError: true,
      ),
    );
    return true;
  }

  bool sendPcm16Chunk(Uint8List bytes) {
    if (_disposed ||
        !_turnActive ||
        _state.phase != AanyaRealtimePhase.listening ||
        _socket == null ||
        bytes.isEmpty ||
        bytes.length > AiraRealtimeProtocol.maxFrameBytes ||
        bytes.length.isOdd) {
      return false;
    }
    if (_turnAudioBytes + bytes.length > maxTurnAudioBytes) {
      unawaited(cancelTurn());
      _setRecoverableError(
        'audio_limit_exceeded',
        'That turn was too long. Please try a shorter message.',
      );
      return false;
    }

    try {
      _socket!.sendBinary(Uint8List.fromList(bytes));
      _turnAudioBytes += bytes.length;
      return true;
    } on Object catch (error) {
      _debugLog('audio send failed: ${error.runtimeType}');
      final socket = _socket;
      if (socket != null) {
        unawaited(_handleUnexpectedDisconnect(socket, _connectionGeneration));
      }
      return false;
    }
  }

  /// Serializes a microphone frame behind all earlier frames and completes
  /// only after the socket sink has accepted it.
  Future<bool> sendPcm16ChunkOrdered(Uint8List bytes) {
    if (_disposed ||
        !_turnActive ||
        _state.phase != AanyaRealtimePhase.listening ||
        _socket == null ||
        bytes.isEmpty ||
        bytes.length > AiraRealtimeProtocol.maxFrameBytes ||
        bytes.length.isOdd) {
      return Future<bool>.value(false);
    }
    if (_turnAudioBytes + _queuedTurnAudioBytes + bytes.length >
        maxTurnAudioBytes) {
      unawaited(cancelTurn());
      _setRecoverableError(
        'audio_limit_exceeded',
        'That turn was too long. Please try a shorter message.',
      );
      return Future<bool>.value(false);
    }
    final payload = Uint8List.fromList(bytes);
    final socket = _socket!;
    final generation = _writeGeneration;
    _queuedTurnAudioBytes += payload.length;
    final completion = Completer<bool>();
    _writeTail = _writeTail.then((_) async {
      if (_disposed ||
          generation != _writeGeneration ||
          socket != _socket ||
          !_turnActive) {
        if (generation == _writeGeneration) {
          _queuedTurnAudioBytes -= payload.length;
        }
        completion.complete(false);
        return;
      }
      try {
        await socket
            .sendBinaryComplete(payload)
            .timeout(const Duration(seconds: 2));
        _queuedTurnAudioBytes -= payload.length;
        _turnAudioBytes += payload.length;
        completion.complete(true);
      } on Object catch (error) {
        _queuedTurnAudioBytes -= payload.length;
        _debugLog('ordered audio send failed: ${error.runtimeType}');
        completion.complete(false);
        unawaited(_handleUnexpectedDisconnect(socket, _connectionGeneration));
      }
    });
    return completion.future;
  }

  /// Sends end_of_turn strictly after all accepted PCM writes.
  Future<bool> endTurnOrdered() async {
    await _writeTail;
    if (_disposed ||
        !_turnActive ||
        _state.phase != AanyaRealtimePhase.listening ||
        _turnAudioBytes == 0 ||
        _queuedTurnAudioBytes != 0 ||
        _socket == null) {
      return false;
    }
    try {
      await _socket!.sendTextComplete(
        const RealtimeEndOfTurnMessage().toWireText(),
      );
    } on Object catch (error) {
      _debugLog('ordered end_of_turn send failed: ${error.runtimeType}');
      return false;
    }
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.finalizing,
        statusLabel: 'Finishing your words',
        partialTranscript: '',
        clearError: true,
      ),
    );
    return true;
  }

  bool endTurn() {
    if (_disposed ||
        !_turnActive ||
        _state.phase != AanyaRealtimePhase.listening ||
        _turnAudioBytes == 0 ||
        _socket == null) {
      return false;
    }
    try {
      _sendControl(const RealtimeEndOfTurnMessage());
    } on Object catch (error) {
      _debugLog('end_of_turn send failed: ${error.runtimeType}');
      return false;
    }
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.finalizing,
        statusLabel: 'Finishing your words',
        partialTranscript: '',
        clearError: true,
      ),
    );
    return true;
  }

  Future<bool> cancelTurn() async {
    if (_disposed || !_turnActive || _socket == null) return false;
    final socket = _socket!;
    final connectionGeneration = _connectionGeneration;
    final acknowledgement = Completer<bool>();
    _cancelAcknowledgement = acknowledgement;
    _cancellingServerTurnId = _activeServerTurnId;
    _resetTurn(preserveCancellation: true);
    if (_state.sessionId != null) {
      _emit(
        _state.copyWith(
          phase: AanyaRealtimePhase.cancelling,
          statusLabel: 'Cancelling',
          partialTranscript: '',
          clearError: true,
        ),
      );
    }
    try {
      socket.sendText(const RealtimeCancelTurnMessage().toWireText());
    } on Object catch (error) {
      _debugLog('cancel_turn send failed: ${error.runtimeType}');
    }
    try {
      return await acknowledgement.future.timeout(cancelAckTimeout);
    } on TimeoutException {
      if (_cancelAcknowledgement == acknowledgement &&
          !_disposed &&
          _socket == socket &&
          _isConnectionCurrent(connectionGeneration)) {
        _debugLog('cancel acknowledgement timed out; recreating session');
        _completeCancellation(false);
        unawaited(_recoverFromCancelTimeout(socket, connectionGeneration));
      }
      return false;
    }
  }

  void clearRecoverableError() {
    if (_disposed ||
        _state.phase != AanyaRealtimePhase.error ||
        !_state.errorIsRecoverable ||
        _state.sessionId == null) {
      return;
    }
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.ready,
        statusLabel: 'Ready',
        clearError: true,
      ),
    );
  }

  /// Flutter visibility is diagnostic only. The active foreground call owns
  /// session lifetime, so Home/shade/lock/app switching never close transport.
  Future<void> setForeground(bool isForeground) async {
    if (_disposed || _isForeground == isForeground) return;
    _isForeground = isForeground;
    _debugLog('flutter_foreground=$isForeground socket_preserved=true');
  }

  Future<void> disconnect() {
    return _disconnectInternal(
      allowFutureReconnect: false,
      statusLabel: 'Offline',
    );
  }

  Future<void> _disconnectInternal({
    required bool allowFutureReconnect,
    required String statusLabel,
  }) async {
    _intentionalClose = true;
    _allowReconnect = false;
    ++_reconnectScheduleGeneration;
    _debugLog('lifecycle event=closing intentional=true');
    _completeCancellation(false);
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    final socket = _socket;
    _socket = null;
    ++_connectionGeneration;
    final subscription = _socketSubscription;
    _socketSubscription = null;

    if (socket != null) {
      try {
        if (_turnActive) {
          socket.sendText(const RealtimeCancelTurnMessage().toWireText());
        }
        socket.sendText(const RealtimeSessionEndMessage().toWireText());
      } on Object {
        // A closing transport may already reject writes.
      }
      await _safeCancelSubscription(subscription);
      await _safeClose(socket);
    }
    _resetTurn();
    _allowReconnect = allowFutureReconnect && !_disposed;
    if (!_disposed) _emitOffline(statusLabel);
    _debugLog('lifecycle event=closed intentional=true');
  }

  void _handleSocketMessage(
    RealtimeWebSocket socket,
    int generation,
    Object? message,
  ) {
    if (_disposed || _socket != socket || !_isConnectionCurrent(generation)) {
      return;
    }
    if (message is String) {
      _handleControlMessage(socket, generation, message);
      return;
    }
    if (message is List<int>) {
      if (_cancelAcknowledgement != null) {
        _debugLog('suppressed stale binary audio during cancellation');
        return;
      }
      _handleBinaryMessage(socket, generation, Uint8List.fromList(message));
      return;
    }
    _protocolFailure(socket, generation, 'unsupported_frame');
  }

  void _handleControlMessage(
    RealtimeWebSocket socket,
    int generation,
    String wireText,
  ) {
    RealtimeServerEvent event;
    try {
      event = RealtimeServerEventParser.parse(wireText);
    } on RealtimeProtocolException catch (error) {
      _debugLog('protocol error: ${error.code}');
      final metadata = _receivedEventMetadata(wireText);
      _protocolFailure(
        socket,
        generation,
        error.code,
        expectedEvent: 'valid_server_event',
        receivedEvent: metadata.event,
        receivedSequence: metadata.sequence,
        turnId: metadata.turnId,
      );
      return;
    }

    if (event is RealtimeSessionReadyEvent) {
      if (_state.phase != AanyaRealtimePhase.aiStarting ||
          _state.sessionId != null) {
        _protocolFailure(
          socket,
          generation,
          'duplicate_session_ready',
          expectedEvent: 'turn_event',
          receivedEvent: 'session_ready',
        );
        return;
      }
      _handshakeTimer?.cancel();
      _handshakeTimer = null;
      if (!event.canProcessTurns) {
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.aiStarting,
            statusLabel: event.readiness.status == 'warming'
                ? 'AI warming up'
                : 'AI unavailable',
            aiDisclosure: event.aiDisclosure,
            clearError: true,
          ),
        );
        _eventController.add(event);
        _debugLog('session transport ready; inference unavailable');
        return;
      }
      _reconnectAttempt = 0;
      _emit(
        _state.copyWith(
          phase: AanyaRealtimePhase.ready,
          statusLabel: 'Ready',
          sessionId: event.sessionId,
          aiDisclosure: event.aiDisclosure,
          clearError: true,
        ),
      );
      _eventController.add(event);
      _timing('session_ready', generation);
      _debugLog('session ready');
      return;
    }

    if (_state.sessionId == null &&
        !(event is RealtimeErrorEvent && !event.recoverable)) {
      _protocolFailure(
        socket,
        generation,
        'event_before_session_ready',
        expectedEvent: 'session_ready',
        receivedEvent: _eventName(event),
      );
      return;
    }
    if (_cancelAcknowledgement != null &&
        (event is RealtimeTurnEvent ||
            (event is RealtimeErrorEvent && event.recoverable))) {
      _handleEventDuringCancellation(socket, generation, event);
      return;
    }
    if (event is RealtimeTurnEvent && !_acceptTurnId(event.turnId)) {
      _protocolFailure(
        socket,
        generation,
        'turn_id_mismatch',
        expectedEvent: 'current_turn_event',
        receivedEvent: _eventName(event),
        turnId: event.turnId,
      );
      return;
    }

    switch (event) {
      case RealtimeSttPartialEvent():
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.finalizing,
            statusLabel: 'Finishing your words',
            partialTranscript: event.text,
          ),
        );
      case RealtimeSttFinalEvent():
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.thinking,
            statusLabel: 'Thinking',
            partialTranscript: '',
            finalTranscript: event.text,
          ),
        );
      case RealtimeThinkingEvent():
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.thinking,
            statusLabel: 'Thinking',
          ),
        );
      case RealtimeTextDeltaEvent():
        final responseText = '${_state.responseText}${event.delta}';
        if (responseText.runes.length >
            AiraRealtimeProtocol.maxTextCharacters) {
          _protocolFailure(socket, generation, 'response_too_large');
          return;
        }
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.thinking,
            statusLabel: 'Thinking',
            responseText: responseText,
          ),
        );
      case RealtimeTextSentenceEvent():
        if (event.sequence != _nextServerSentenceSequence) {
          _protocolFailure(
            socket,
            generation,
            'invalid_sentence_sequence',
            expectedEvent: 'text_sentence',
            receivedEvent: 'text_sentence',
            expectedSequence: _nextServerSentenceSequence,
            receivedSequence: event.sequence,
            turnId: event.turnId,
          );
          return;
        }
        _nextServerSentenceSequence += 1;
        if (_state.responseText.isEmpty) {
          _emit(_state.copyWith(responseText: event.text));
        }
      case RealtimeAudioChunkEvent():
        if (_playbackSampleRateHz != null &&
            event.audioFormat.sampleRateHz != _playbackSampleRateHz) {
          _protocolFailure(socket, generation, 'playback_sample_rate_changed');
          return;
        }
        if (_pendingServerAudio != null ||
            event.sequence != _nextServerAudioSequence) {
          _protocolFailure(
            socket,
            generation,
            'invalid_audio_sequence',
            expectedEvent: _pendingServerAudio == null
                ? 'audio_chunk'
                : 'binary_audio',
            receivedEvent: 'audio_chunk',
            expectedSequence: _nextServerAudioSequence,
            receivedSequence: event.sequence,
            turnId: event.turnId,
          );
          return;
        }
        _playbackSampleRateHz ??= event.audioFormat.sampleRateHz;
        _pendingServerAudio = event;
      case RealtimeSpeakingEvent():
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.speaking,
            statusLabel: 'Speaking',
          ),
        );
      case RealtimeTurnCompleteEvent():
        if (_pendingServerAudio != null) {
          _protocolFailure(
            socket,
            generation,
            'missing_audio_frame',
            expectedEvent: 'binary_audio',
            receivedEvent: 'turn_complete',
            expectedSequence: _nextServerAudioSequence,
            turnId: event.turnId,
          );
          return;
        }
        _resetTurn();
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.ready,
            statusLabel: 'Ready',
            partialTranscript: '',
            clearError: true,
          ),
        );
      case RealtimeErrorEvent():
        _resetTurn();
        if (event.recoverable && event.code == 'turn_cancelled') {
          if (_state.phase != AanyaRealtimePhase.error) {
            _emit(
              _state.copyWith(
                phase: AanyaRealtimePhase.ready,
                statusLabel: 'Ready',
                partialTranscript: '',
                clearError: true,
              ),
            );
          }
        } else if (event.recoverable) {
          _setRecoverableError(event.code, event.message);
        } else {
          _allowReconnect = false;
          _emit(
            _state.copyWith(
              phase: AanyaRealtimePhase.error,
              statusLabel: event.message,
              errorCode: event.code,
              errorIsRecoverable: false,
            ),
          );
          unawaited(_closeAfterFatalEvent(socket, generation));
        }
      case RealtimeSessionReadyEvent():
        // Handled before the session-state guard above.
        break;
    }
    if (!_eventController.isClosed) _eventController.add(event);
  }

  void _handleEventDuringCancellation(
    RealtimeWebSocket socket,
    int generation,
    RealtimeServerEvent event,
  ) {
    if (event case RealtimeErrorEvent(code: 'turn_cancelled')) {
      final expectedTurnId = _cancellingServerTurnId;
      if (expectedTurnId != null && event.turnId != expectedTurnId) {
        _protocolFailure(
          socket,
          generation,
          'turn_id_mismatch',
          expectedEvent: 'turn_cancelled',
          receivedEvent: 'turn_cancelled',
          turnId: event.turnId,
        );
        return;
      }
      final preserveRecoverableError =
          _state.phase == AanyaRealtimePhase.error && _state.errorIsRecoverable;
      _completeCancellation(true);
      _resetTurn();
      if (!preserveRecoverableError) {
        _emit(
          _state.copyWith(
            phase: AanyaRealtimePhase.ready,
            statusLabel: 'Ready',
            partialTranscript: '',
            clearError: true,
          ),
        );
      }
      if (!_eventController.isClosed) _eventController.add(event);
      return;
    }
    final staleTurnId = switch (event) {
      RealtimeTurnEvent(:final turnId) => turnId,
      RealtimeErrorEvent(:final turnId) => turnId,
      _ => null,
    };
    _debugLog(
      'suppressed stale event during cancellation '
      'received_event=${_eventName(event)} turn_id=${staleTurnId ?? 'none'}',
    );
  }

  void _handleBinaryMessage(
    RealtimeWebSocket socket,
    int generation,
    Uint8List bytes,
  ) {
    final header = _pendingServerAudio;
    if (header == null ||
        bytes.length != header.byteLength ||
        bytes.length > AiraRealtimeProtocol.maxFrameBytes ||
        bytes.length.isOdd) {
      _protocolFailure(
        socket,
        generation,
        'unexpected_audio_frame',
        expectedEvent: header == null ? 'server_event' : 'binary_audio',
        receivedEvent: 'binary_audio',
        expectedSequence: header?.sequence,
        receivedSequence: _nextServerAudioSequence,
        turnId: header?.turnId,
      );
      return;
    }
    _pendingServerAudio = null;
    if (_serverAudioBytes + bytes.length >
        AiraRealtimeProtocol.maxTurnAudioBytes) {
      _protocolFailure(socket, generation, 'response_audio_too_large');
      return;
    }
    _serverAudioBytes += bytes.length;
    _nextServerAudioSequence += 1;
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.speaking,
        statusLabel: 'Speaking',
      ),
    );
    if (!_audioController.isClosed) {
      _audioController.add(RealtimeAudioFrame(header: header, bytes: bytes));
    }
  }

  bool _acceptTurnId(String turnId) {
    final current = _activeServerTurnId;
    if (current == null) {
      if (!_turnActive) return false;
      _activeServerTurnId = turnId;
      return true;
    }
    return current == turnId;
  }

  void _sendControl(RealtimeClientMessage message) {
    final wireText = message.toWireText();
    if (wireText.length > AiraRealtimeProtocol.maxClientControlBytes) {
      throw const RealtimeProtocolException(
        'message_too_large',
        'Client control message exceeded its safe limit.',
      );
    }
    final socket = _socket;
    if (socket == null) throw StateError('Realtime socket is not connected.');
    socket.sendText(wireText);
  }

  void _protocolFailure(
    RealtimeWebSocket socket,
    int generation,
    String code, {
    String? expectedEvent,
    String? receivedEvent,
    int? expectedSequence,
    int? receivedSequence,
    String? turnId,
  }) {
    final violation = RealtimeProtocolViolation(
      code: code,
      expectedEvent: expectedEvent,
      receivedEvent: receivedEvent,
      expectedSequence: expectedSequence,
      receivedSequence: receivedSequence,
      sessionId: _state.sessionId,
      turnId: turnId ?? _activeServerTurnId,
      textSequence: _nextServerSentenceSequence,
      audioSequence: _nextServerAudioSequence,
    );
    _lastProtocolViolation = violation;
    _debugLog(
      'protocol_violation_code=${violation.code} '
      'expected_event=${violation.expectedEvent ?? 'none'} '
      'received_event=${violation.receivedEvent ?? 'none'} '
      'expected_sequence=${violation.expectedSequence ?? -1} '
      'received_sequence=${violation.receivedSequence ?? -1} '
      'session_id=${violation.sessionId ?? 'none'} '
      'turn_id=${violation.turnId ?? 'none'} '
      'text_seq=${violation.textSequence} '
      'audio_seq=${violation.audioSequence}',
    );
    _allowReconnect = false;
    _completeCancellation(false);
    _resetTurn();
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.error,
        statusLabel: 'Realtime protocol error',
        errorCode: code,
        errorIsRecoverable: false,
      ),
    );
    unawaited(_closeAfterFatalEvent(socket, generation));
  }

  Future<void> _closeAfterFatalEvent(
    RealtimeWebSocket socket,
    int generation,
  ) async {
    if (_socket != socket || !_isConnectionCurrent(generation)) return;
    _socket = null;
    ++_connectionGeneration;
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    final subscription = _socketSubscription;
    _socketSubscription = null;
    await subscription?.cancel();
    await _safeClose(socket, 1002, 'protocol error');
  }

  Future<void> _handleUnexpectedDisconnect(
    RealtimeWebSocket socket,
    int generation,
  ) async {
    if (_socket != socket || !_isConnectionCurrent(generation)) return;
    _completeCancellation(false);
    _socket = null;
    ++_connectionGeneration;
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    final subscription = _socketSubscription;
    _socketSubscription = null;
    _resetTurn();
    await _safeCancelSubscription(subscription);
    await _safeClose(socket);
    if (_disposed) return;
    final shouldReconnect = _allowReconnect && !_intentionalClose;
    _emitOffline(shouldReconnect ? 'Recovering' : 'Offline');
    if (shouldReconnect) _scheduleReconnect();
  }

  Future<void> _recoverFromCancelTimeout(
    RealtimeWebSocket socket,
    int generation,
  ) async {
    if (_disposed || _socket != socket || !_isConnectionCurrent(generation)) {
      return;
    }
    _socket = null;
    ++_connectionGeneration;
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    final subscription = _socketSubscription;
    _socketSubscription = null;
    _resetTurn();
    _allowReconnect = true;
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.recovering,
        statusLabel: 'Recovering',
        clearSession: true,
        clearError: true,
      ),
    );
    await _safeCancelSubscription(subscription);
    await _safeClose(socket, 1001, 'cancel acknowledgement timeout');
    if (!_disposed && _allowReconnect && _socket == null) {
      await connect();
    }
  }

  void _scheduleReconnect() {
    if (_disposed ||
        _intentionalClose ||
        !_allowReconnect ||
        _socket != null ||
        _connectInFlight != null ||
        _reconnectInFlight != null) {
      return;
    }
    if (reconnectDelays.isEmpty) {
      _emitOffline('Offline');
      return;
    }
    if (!repeatLastReconnectDelay &&
        _reconnectAttempt >= reconnectDelays.length) {
      _emitOffline('Offline');
      return;
    }
    final reconnectIndex = _reconnectAttempt.clamp(
      0,
      reconnectDelays.length - 1,
    );
    final delay = reconnectDelays[reconnectIndex];
    final scheduleGeneration = _reconnectScheduleGeneration;
    _reconnectAttempt += 1;
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.recovering,
        statusLabel: 'Recovering',
        clearSession: true,
      ),
    );
    _debugLog(
      'lifecycle event=reconnect_scheduled intentional=false '
      'schedule_generation=$scheduleGeneration delay_ms=${delay.inMilliseconds}',
    );
    final operation = () async {
      await _reconnectDelay(delay);
      if (!_disposed &&
          !_intentionalClose &&
          _allowReconnect &&
          scheduleGeneration == _reconnectScheduleGeneration &&
          _socket == null) {
        await connect();
      }
    }();
    _reconnectInFlight = operation;
    unawaited(
      operation.whenComplete(() {
        if (identical(_reconnectInFlight, operation)) {
          _reconnectInFlight = null;
        }
        if (!_disposed &&
            !_intentionalClose &&
            _allowReconnect &&
            scheduleGeneration == _reconnectScheduleGeneration &&
            _socket == null &&
            _connectInFlight == null) {
          _scheduleReconnect();
        }
      }),
    );
  }

  void _setRecoverableError(String code, String message) {
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.error,
        statusLabel: message,
        errorCode: code,
        errorIsRecoverable: true,
      ),
    );
  }

  void _emitOffline(String statusLabel) {
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.offline,
        statusLabel: statusLabel,
        clearSession: true,
        clearError: true,
      ),
    );
  }

  void _resetTurn({bool preserveCancellation = false}) {
    _writeGeneration += 1;
    _turnActive = false;
    _turnAudioBytes = 0;
    _queuedTurnAudioBytes = 0;
    _serverAudioBytes = 0;
    _activeServerTurnId = null;
    _pendingServerAudio = null;
    _nextServerAudioSequence = 0;
    _nextServerSentenceSequence = 1;
    _playbackSampleRateHz = null;
    if (!preserveCancellation) _cancellingServerTurnId = null;
  }

  void _completeCancellation(bool acknowledged) {
    final completion = _cancelAcknowledgement;
    _cancelAcknowledgement = null;
    _cancellingServerTurnId = null;
    if (completion != null && !completion.isCompleted) {
      completion.complete(acknowledged);
    }
  }

  bool _isConnectionCurrent(int generation) =>
      !_disposed && generation == _connectionGeneration;

  Future<void> _safeClose(
    RealtimeWebSocket socket, [
    int? code,
    String? reason,
  ]) async {
    try {
      await socket.close(code, reason).timeout(closeTimeout);
    } on Object {
      // Cleanup is best-effort when the peer has already disappeared.
    }
  }

  Future<void> _safeCancelSubscription(
    StreamSubscription<Object?>? subscription,
  ) async {
    try {
      await subscription?.cancel().timeout(closeTimeout);
    } on Object {
      // Ownership is already invalidated by the connection generation.
    }
  }

  void _emit(AanyaRealtimeState next) {
    if (_disposed) return;
    _state = next;
    notifyListeners();
  }

  void _debugLog(String message) {
    if (kDebugMode) debugPrint('[AIRA REALTIME] $message');
  }

  void _timing(String event, int generation) {
    _debugLog(
      'timing event=$event session_id=${_state.sessionId ?? 'none'} '
      'turn_id=${_activeServerTurnId ?? 'none'} generation=$generation '
      'monotonic_us=${_monotonicClock.elapsedMicroseconds}',
    );
  }

  @override
  void dispose() {
    if (_disposed) return;
    _intentionalClose = true;
    _allowReconnect = false;
    ++_reconnectScheduleGeneration;
    _completeCancellation(false);
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    _state = _state.copyWith(
      phase: AanyaRealtimePhase.disposed,
      statusLabel: 'Voice session closed',
      clearSession: true,
      clearError: true,
    );
    notifyListeners();
    _disposed = true;
    _shutdownComplete = _shutdown();
    super.dispose();
  }

  Future<void> _shutdown() async {
    final socket = _socket;
    _socket = null;
    ++_connectionGeneration;
    final subscription = _socketSubscription;
    _socketSubscription = null;
    if (socket != null) {
      try {
        if (_turnActive) {
          socket.sendText(const RealtimeCancelTurnMessage().toWireText());
        }
        socket.sendText(const RealtimeSessionEndMessage().toWireText());
      } on Object {
        // The transport may already be gone.
      }
      await _safeCancelSubscription(subscription);
      await _safeClose(socket);
    }
    _resetTurn();
    await _eventController.close();
    await _audioController.close();
  }

  static Future<void> _defaultReconnectDelay(Duration duration) =>
      Future<void>.delayed(duration);
}

final class _ReceivedEventMetadata {
  const _ReceivedEventMetadata({this.event, this.sequence, this.turnId});

  final String? event;
  final int? sequence;
  final String? turnId;
}

_ReceivedEventMetadata _receivedEventMetadata(String wireText) {
  try {
    final value = jsonDecode(wireText);
    if (value is! Map) return const _ReceivedEventMetadata();
    return _ReceivedEventMetadata(
      event: value['type'] as String?,
      sequence: value['sequence'] as int?,
      turnId: value['turn_id'] as String?,
    );
  } on Object {
    return const _ReceivedEventMetadata();
  }
}

String _eventName(RealtimeServerEvent event) => switch (event) {
  RealtimeSessionReadyEvent() => 'session_ready',
  RealtimeSttPartialEvent() => 'stt_partial',
  RealtimeSttFinalEvent() => 'stt_final',
  RealtimeThinkingEvent() => 'thinking',
  RealtimeTextDeltaEvent() => 'text_delta',
  RealtimeTextSentenceEvent() => 'text_sentence',
  RealtimeAudioChunkEvent() => 'audio_chunk',
  RealtimeSpeakingEvent() => 'speaking',
  RealtimeTurnCompleteEvent() => 'turn_complete',
  RealtimeErrorEvent(:final recoverable) =>
    recoverable ? 'recoverable_error' : 'fatal_error',
};
