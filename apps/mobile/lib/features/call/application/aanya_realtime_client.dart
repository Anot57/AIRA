import 'dart:async';

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
    this.maxTurnAudioBytes = AiraRealtimeProtocol.maxTurnAudioBytes,
    List<Duration> reconnectDelays = const <Duration>[
      Duration(milliseconds: 500),
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 4),
    ],
    ReconnectDelay? reconnectDelay,
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
  final int maxTurnAudioBytes;
  final List<Duration> reconnectDelays;

  final StreamController<RealtimeServerEvent> _eventController =
      StreamController<RealtimeServerEvent>.broadcast(sync: true);
  final StreamController<RealtimeAudioFrame> _audioController =
      StreamController<RealtimeAudioFrame>.broadcast(sync: true);

  AanyaRealtimeState _state = const AanyaRealtimeState.offline();
  AanyaRealtimeState get state => _state;

  Stream<RealtimeServerEvent> get events => _eventController.stream;
  Stream<RealtimeAudioFrame> get audioFrames => _audioController.stream;

  RealtimeWebSocket? _socket;
  StreamSubscription<Object?>? _socketSubscription;
  Future<void>? _connectInFlight;
  Future<void>? _reconnectInFlight;
  Timer? _handshakeTimer;
  RealtimeAudioChunkEvent? _pendingServerAudio;
  var _connectionGeneration = 0;
  var _reconnectAttempt = 0;
  var _nextServerAudioSequence = 0;
  var _nextServerSentenceSequence = 1;
  var _turnAudioBytes = 0;
  var _turnActive = false;
  String? _activeServerTurnId;
  var _allowReconnect = false;
  var _isForeground = true;
  var _disposed = false;
  Future<void> _shutdownComplete = Future<void>.value();

  Future<void> get shutdownComplete => _shutdownComplete;

  /// Opens at most one socket. Reaching [AanyaRealtimePhase.aiStarting] means
  /// the handshake was sent; only session_ready transitions the client to
  /// [AanyaRealtimePhase.ready].
  Future<void> connect() {
    if (_disposed || !_isForeground) return Future<void>.value();
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
        if (!_disposed && _allowReconnect && _isForeground && _socket == null) {
          _scheduleReconnect();
        }
      }),
    );
    return operation;
  }

  Future<void> _connectOnce(int generation) async {
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
    try {
      _sendControl(const RealtimeCancelTurnMessage());
    } on Object catch (error) {
      _debugLog('cancel_turn send failed: ${error.runtimeType}');
    }
    _resetTurn();
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
    return true;
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

  /// Backgrounding closes the microphone session and suppresses reconnects.
  /// Foregrounding starts one fresh protocol handshake.
  Future<void> setForeground(bool isForeground) async {
    if (_disposed || _isForeground == isForeground) return;
    _isForeground = isForeground;
    if (!isForeground) {
      await _disconnectInternal(
        allowFutureReconnect: true,
        statusLabel: 'Offline',
      );
      return;
    }
    _allowReconnect = true;
    await connect();
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
    _allowReconnect = false;
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
      await subscription?.cancel();
      await _safeClose(socket);
    }
    _resetTurn();
    _allowReconnect = allowFutureReconnect && !_disposed;
    if (!_disposed) _emitOffline(statusLabel);
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
      _protocolFailure(socket, generation, error.code);
      return;
    }

    if (event is RealtimeSessionReadyEvent) {
      if (_state.phase != AanyaRealtimePhase.aiStarting ||
          _state.sessionId != null) {
        _protocolFailure(socket, generation, 'duplicate_session_ready');
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
      _debugLog('session ready');
      return;
    }

    if (_state.sessionId == null &&
        !(event is RealtimeErrorEvent && !event.recoverable)) {
      _protocolFailure(socket, generation, 'event_before_session_ready');
      return;
    }
    if (event is RealtimeTurnEvent && !_acceptTurnId(event.turnId)) {
      _protocolFailure(socket, generation, 'turn_id_mismatch');
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
          _protocolFailure(socket, generation, 'invalid_sentence_sequence');
          return;
        }
        _nextServerSentenceSequence += 1;
        if (_state.responseText.isEmpty) {
          _emit(_state.copyWith(responseText: event.text));
        }
      case RealtimeAudioChunkEvent():
        if (_pendingServerAudio != null ||
            event.sequence != _nextServerAudioSequence) {
          _protocolFailure(socket, generation, 'invalid_audio_sequence');
          return;
        }
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
          _protocolFailure(socket, generation, 'missing_audio_frame');
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
      _protocolFailure(socket, generation, 'unexpected_audio_frame');
      return;
    }
    _pendingServerAudio = null;
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

  void _protocolFailure(RealtimeWebSocket socket, int generation, String code) {
    _allowReconnect = false;
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
    _socket = null;
    ++_connectionGeneration;
    _handshakeTimer?.cancel();
    _handshakeTimer = null;
    final subscription = _socketSubscription;
    _socketSubscription = null;
    _resetTurn();
    await subscription?.cancel();
    await _safeClose(socket);
    if (_disposed) return;
    _emitOffline(_allowReconnect ? 'Recovering' : 'Offline');
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_disposed ||
        !_allowReconnect ||
        !_isForeground ||
        _socket != null ||
        _connectInFlight != null ||
        _reconnectInFlight != null) {
      return;
    }
    if (_reconnectAttempt >= reconnectDelays.length) {
      _emitOffline('Offline');
      return;
    }
    final delay = reconnectDelays[_reconnectAttempt];
    _reconnectAttempt += 1;
    _emit(
      _state.copyWith(
        phase: AanyaRealtimePhase.recovering,
        statusLabel: 'Recovering',
        clearSession: true,
      ),
    );
    final operation = () async {
      await _reconnectDelay(delay);
      if (!_disposed && _allowReconnect && _isForeground && _socket == null) {
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
            _allowReconnect &&
            _isForeground &&
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

  void _resetTurn() {
    _turnActive = false;
    _turnAudioBytes = 0;
    _activeServerTurnId = null;
    _pendingServerAudio = null;
    _nextServerAudioSequence = 0;
    _nextServerSentenceSequence = 1;
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

  void _emit(AanyaRealtimeState next) {
    if (_disposed) return;
    _state = next;
    notifyListeners();
  }

  void _debugLog(String message) {
    if (kDebugMode) debugPrint('[AIRA REALTIME] $message');
  }

  @override
  void dispose() {
    if (_disposed) return;
    _allowReconnect = false;
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
      await subscription?.cancel();
      await _safeClose(socket);
    }
    _resetTurn();
    await _eventController.close();
    await _audioController.close();
  }

  static Future<void> _defaultReconnectDelay(Duration duration) =>
      Future<void>.delayed(duration);
}
