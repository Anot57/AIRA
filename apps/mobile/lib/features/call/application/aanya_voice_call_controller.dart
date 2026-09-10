import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../data/conversation_api.dart';
import '../domain/conversation_session_turn.dart';
import '../domain/conversation_turn_response.dart';
import 'voice_io.dart';

enum VoiceCallPhase {
  idle,
  connecting,
  ready,
  listening,
  finalizingUserTurn,
  thinking,
  speaking,
  reconnecting,
  ending,
  ended,
  checkingConnection,
  recording,
  processing,
  playing,
  error,
  disposed,
}

enum LocalAiConnection { unknown, checking, connected, offline }

/// The single immutable source of truth for the Aanya voice-call screen.
final class VoiceCallState {
  VoiceCallState({
    required this.phase,
    required this.connection,
    required this.statusLabel,
    required List<ConversationSessionTurn> turns,
    this.userMessage,
    this.callActive = false,
    this.callStartedAt,
    this.callGeneration = 0,
  }) : turns = List<ConversationSessionTurn>.unmodifiable(turns);

  factory VoiceCallState.initial() => VoiceCallState(
    phase: VoiceCallPhase.idle,
    connection: LocalAiConnection.unknown,
    statusLabel: 'Preparing local voice…',
    turns: const <ConversationSessionTurn>[],
  );

  final VoiceCallPhase phase;
  final LocalAiConnection connection;
  final String statusLabel;
  final List<ConversationSessionTurn> turns;
  final String? userMessage;
  final bool callActive;
  final DateTime? callStartedAt;
  final int callGeneration;

  bool get canStartCall =>
      !callActive &&
      phase != VoiceCallPhase.connecting &&
      phase != VoiceCallPhase.ending &&
      phase != VoiceCallPhase.disposed;

  bool get canEndCall => callActive && phase != VoiceCallPhase.ending;

  bool get canStartRecording =>
      connection == LocalAiConnection.connected &&
      (phase == VoiceCallPhase.idle || phase == VoiceCallPhase.error);

  bool get canRetryConnection =>
      connection == LocalAiConnection.offline &&
      (phase == VoiceCallPhase.idle || phase == VoiceCallPhase.error);

  bool get canCancelTurn =>
      phase == VoiceCallPhase.processing || phase == VoiceCallPhase.playing;

  VoiceCallState copyWith({
    VoiceCallPhase? phase,
    LocalAiConnection? connection,
    String? statusLabel,
    List<ConversationSessionTurn>? turns,
    String? userMessage,
    bool clearUserMessage = false,
    bool? callActive,
    DateTime? callStartedAt,
    int? callGeneration,
    bool clearCallStartedAt = false,
  }) {
    return VoiceCallState(
      phase: phase ?? this.phase,
      connection: connection ?? this.connection,
      statusLabel: statusLabel ?? this.statusLabel,
      turns: turns ?? this.turns,
      userMessage: clearUserMessage ? null : (userMessage ?? this.userMessage),
      callActive: callActive ?? this.callActive,
      callStartedAt: clearCallStartedAt
          ? null
          : (callStartedAt ?? this.callStartedAt),
      callGeneration: callGeneration ?? this.callGeneration,
    );
  }
}

abstract interface class AanyaVoiceCallCoordinator implements Listenable {
  VoiceCallState get state;
  Future<void> get shutdownComplete;
  Future<void> initialize();
  Future<bool> startCall();
  Future<void> endCall();
  Future<void> retryConnection();
  Future<bool> beginRecording();
  Future<void> finishRecording();
  Future<void> cancelRecording();
  Future<void> cancelActiveTurn();
  Future<void> handleAppBackgrounded();
  void handleAppResumed();
  void clearRecoverableError();
  void dispose();
}

/// Coordinates one whole-WAV -> HTTP -> generated-WAV local voice turn.
final class AanyaVoiceCallController extends ChangeNotifier
    implements AanyaVoiceCallCoordinator {
  AanyaVoiceCallController({
    required String companionId,
    required this.api,
    required this.recorder,
    required this.playback,
    required this.recordingStore,
    DateTime Function()? now,
    this.minimumRecordingDuration = const Duration(milliseconds: 400),
  }) : _now = now ?? DateTime.now {
    if (companionId != supportedCompanionId) {
      throw ArgumentError.value(
        companionId,
        'companionId',
        'Local voice is available only for aanya.',
      );
    }
  }

  static const String supportedCompanionId = 'aanya';
  static const String backendUnavailableMessage =
      "Aira can't reach the local AI service. Make sure your laptop and phone "
      'are on the same Wi-Fi and the local Aira server is running.';

  final ConversationApi api;
  final VoiceRecorder recorder;
  final VoicePlayback playback;
  final TemporaryRecordingStore recordingStore;
  final DateTime Function() _now;
  final Duration minimumRecordingDuration;

  VoiceCallState _state = VoiceCallState.initial();
  @override
  VoiceCallState get state => _state;

  _RecordingAttempt? _recordingAttempt;
  var _operationGeneration = 0;
  var _initialized = false;
  var _disposed = false;
  var _isForeground = true;
  Future<void> _shutdownComplete = Future<void>.value();

  @override
  Future<void> get shutdownComplete => _shutdownComplete;

  static bool supportsCompanion(String companionId) =>
      companionId == supportedCompanionId;

  @override
  Future<void> initialize() async {
    if (_initialized || _disposed) return;
    _initialized = true;
    await _checkConnection();
  }

  @override
  Future<void> retryConnection() async {
    if (_disposed || !_state.canRetryConnection) return;
    await _checkConnection();
  }

  @override
  Future<bool> startCall() async {
    if (_disposed || _state.callActive) return false;
    _emit(
      _state.copyWith(
        callActive: true,
        callStartedAt: _now(),
        callGeneration: _operationGeneration + 1,
        clearUserMessage: true,
      ),
    );
    final started = await beginRecording();
    if (!started && !_disposed) {
      _emit(
        _state.copyWith(callActive: false, clearCallStartedAt: true),
      );
    }
    return started;
  }

  @override
  Future<void> endCall() async {
    if (_disposed || !_state.callActive) return;
    await cancelActiveTurn();
    if (!_disposed) {
      _emit(
        _state.copyWith(
          phase: VoiceCallPhase.ended,
          statusLabel: 'Call ended',
          callActive: false,
          clearCallStartedAt: true,
          clearUserMessage: true,
        ),
      );
    }
  }

  Future<void> _checkConnection() async {
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.checkingConnection,
        connection: LocalAiConnection.checking,
        statusLabel: 'Checking local AI…',
        clearUserMessage: true,
      ),
    );
    var reachable = false;
    try {
      reachable = await api.checkHealth();
    } on Object {
      reachable = false;
    }
    if (_disposed) return;

    _debugLog('health check: ${reachable ? 'connected' : 'offline'}');
    if (reachable) {
      _emit(
        _state.copyWith(
          phase: VoiceCallPhase.idle,
          connection: LocalAiConnection.connected,
          statusLabel: 'Ready to start',
          clearUserMessage: true,
        ),
      );
    } else {
      _fail(
        backendUnavailableMessage,
        connection: LocalAiConnection.offline,
        category: 'backend_unavailable',
      );
    }
  }

  @override
  Future<bool> beginRecording() async {
    if (_disposed || !_state.canStartRecording) return false;

    final attempt = _RecordingAttempt(++_operationGeneration);
    _recordingAttempt = attempt;
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.recording,
        statusLabel: 'Starting microphone…',
        clearUserMessage: true,
      ),
    );

    try {
      await playback.stop();
      if (!_isCurrent(attempt)) return false;

      final permitted = await recorder.hasPermission();
      if (!_isCurrent(attempt)) return false;
      if (!permitted) {
        _recordingAttempt = null;
        _fail(
          'Microphone permission is needed to talk with Aanya. You can allow '
          'it in Android settings and try again.',
          category: 'permission_denied',
        );
        return false;
      }

      attempt.path = await recordingStore.allocateWavPath();
      if (!_isCurrent(attempt)) {
        await _deleteAttemptPath(attempt);
        return false;
      }

      await recorder.startWav(attempt.path!);
      attempt
        ..started = true
        ..startedAt = _now();
      if (!_isCurrent(attempt)) {
        await _safeRecorderCancel();
        await _deleteAttemptPath(attempt);
        return false;
      }

      _debugLog('state recording: microphone started');
      _emit(
        _state.copyWith(
          phase: VoiceCallPhase.recording,
          statusLabel: 'Listening…',
          clearUserMessage: true,
        ),
      );
      if (attempt.finishRequested) {
        unawaited(_finishAttempt(attempt));
      }
      return true;
    } on Object {
      if (attempt.started) await _safeRecorderCancel();
      await _deleteAttemptPath(attempt);
      if (_isCurrent(attempt)) {
        _recordingAttempt = null;
        _fail(
          "Aira couldn't start the microphone. Please try again.",
          category: 'recorder_start_failed',
        );
      }
      return false;
    }
  }

  @override
  Future<void> finishRecording() async {
    final attempt = _recordingAttempt;
    if (_disposed ||
        attempt == null ||
        _state.phase != VoiceCallPhase.recording ||
        attempt.cancelRequested ||
        attempt.finishing) {
      return;
    }

    attempt.finishRequested = true;
    if (attempt.started) await _finishAttempt(attempt);
  }

  Future<void> _finishAttempt(_RecordingAttempt attempt) async {
    if (attempt.finishing || !_isCurrent(attempt)) return;
    attempt.finishing = true;
    final recordingStoppedAt = _now();
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.processing,
        statusLabel: 'Understanding…',
        clearUserMessage: true,
      ),
    );

    String? stoppedPath;
    try {
      stoppedPath = await recorder.stop();
    } on Object {
      await _deleteAttemptPath(attempt);
      if (_isCurrent(attempt)) {
        _recordingAttempt = null;
        _fail(
          "Aira couldn't finish that recording. Please try again.",
          category: 'recorder_stop_failed',
        );
      }
      return;
    }

    if (!_isCurrent(attempt)) {
      await _deleteAttemptPath(attempt);
      return;
    }
    final expectedPath = attempt.path;
    final startedAt = attempt.startedAt;
    if (stoppedPath == null || expectedPath == null || startedAt == null) {
      await _deleteAttemptPath(attempt);
      _recordingAttempt = null;
      _fail(
        "Aira couldn't finish that recording. Please try again.",
        category: 'recorder_returned_no_file',
      );
      return;
    }
    if (File(stoppedPath).absolute.path != File(expectedPath).absolute.path) {
      await _deleteAttemptPath(attempt);
      _recordingAttempt = null;
      _fail(
        "Aira couldn't verify that recording. Please try again.",
        category: 'recorder_path_mismatch',
      );
      return;
    }

    final duration = recordingStoppedAt.difference(startedAt);
    if (duration < minimumRecordingDuration) {
      await _deleteAttemptPath(attempt);
      _recordingAttempt = null;
      _fail(
        'That recording was too short to process.',
        category: 'recording_too_short',
      );
      return;
    }

    _recordingAttempt = null;
    final requestStartedAt = _now();
    _debugLog('conversation request: started');

    ConversationTurnResponse response;
    try {
      response = await api.createAanyaTurn(expectedPath);
    } on ConversationApiException catch (error) {
      await _safeDelete(expectedPath);
      if (_isOperationCurrent(attempt.token)) _handleApiError(error);
      return;
    } on Object {
      await _safeDelete(expectedPath);
      if (_isOperationCurrent(attempt.token)) {
        _fail(
          'Aira received an unexpected response from the local AI service.',
          category: 'unexpected_response',
        );
      }
      return;
    }
    await _safeDelete(expectedPath);
    final responseReceivedAt = _now();
    if (!_isOperationCurrent(attempt.token)) return;

    _debugLog(
      'conversation request: completed in '
      '${responseReceivedAt.difference(requestStartedAt).inMilliseconds} ms; '
      'turn_id=${response.turnId}',
    );
    final updatedTurns = <ConversationSessionTurn>[
      ..._state.turns,
      ConversationSessionTurn.fromResponse(response),
    ];
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.processing,
        statusLabel: "Preparing Aanya's voice…",
        turns: updatedTurns,
        clearUserMessage: true,
      ),
    );

    if (!_isForeground) {
      _fail(
        "Aanya's response is ready, but playback was skipped while Aira was "
        'in the background.',
        category: 'background_playback_skipped',
      );
      return;
    }

    DateTime? playbackStartedAt;
    try {
      await playback.play(
        response.audioUri,
        onPlaybackStarted: () {
          if (!_isOperationCurrent(attempt.token) || !_isForeground) return;
          playbackStartedAt ??= _now();
          _emit(
            _state.copyWith(
              phase: VoiceCallPhase.playing,
              statusLabel: 'Aanya is speaking…',
              clearUserMessage: true,
            ),
          );
          _debugLog('audio playback: started; turn_id=${response.turnId}');
          _logLatency(
            recordingStoppedAt: recordingStoppedAt,
            responseReceivedAt: responseReceivedAt,
            playbackStartedAt: playbackStartedAt!,
          );
        },
      );
    } on Object {
      if (_isOperationCurrent(attempt.token)) {
        _fail(
          "Aanya's response is ready, but the audio couldn't play.",
          category: 'playback_failed',
        );
      }
      return;
    }

    if (_isOperationCurrent(attempt.token)) {
      _debugLog('audio playback: completed; turn_id=${response.turnId}');
      _emit(
        _state.copyWith(
          phase: VoiceCallPhase.idle,
          statusLabel: 'Call active',
          clearUserMessage: true,
        ),
      );
    }
  }

  @override
  Future<void> cancelRecording() async {
    final attempt = _recordingAttempt;
    if (_disposed || attempt == null) return;
    await _cancelAttempt(attempt, returnToIdle: true);
  }

  @override
  Future<void> cancelActiveTurn() => cancelRecording();

  Future<void> _cancelAttempt(
    _RecordingAttempt attempt, {
    required bool returnToIdle,
  }) {
    attempt.cancelRequested = true;
    return attempt.cancellation ??= () async {
      if (attempt.started) await _safeRecorderCancel();
      await _deleteAttemptPath(attempt);
      if (identical(_recordingAttempt, attempt)) {
        _recordingAttempt = null;
        if (attempt.token == _operationGeneration) _operationGeneration += 1;
        if (returnToIdle && !_disposed) {
          _emit(
            _state.copyWith(
              phase: VoiceCallPhase.idle,
              statusLabel: 'Call active',
              clearUserMessage: true,
            ),
          );
        }
      }
    }();
  }

  @override
  Future<void> handleAppBackgrounded() async {
    if (_disposed) return;
    _isForeground = false;
    if (_state.phase == VoiceCallPhase.recording) {
      await cancelRecording();
      return;
    }
    if (_state.phase == VoiceCallPhase.playing) {
      _operationGeneration += 1;
      await _safePlaybackStop();
      if (!_disposed) {
        _emit(
          _state.copyWith(
            phase: VoiceCallPhase.idle,
            statusLabel: 'Call active',
            clearUserMessage: true,
          ),
        );
      }
    }
  }

  @override
  void handleAppResumed() {
    if (!_disposed) _isForeground = true;
  }

  @override
  void clearRecoverableError() {
    if (_disposed ||
        _state.phase != VoiceCallPhase.error ||
        _state.connection != LocalAiConnection.connected) {
      return;
    }
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.idle,
        statusLabel: _state.callActive ? 'Call active' : 'Ready to start',
        clearUserMessage: true,
      ),
    );
  }

  void _handleApiError(ConversationApiException error) {
    switch (error.kind) {
      case ConversationApiErrorKind.invalidRequest:
        _fail(
          "Aira couldn't use that recording. Please record it again.",
          category: 'invalid_audio',
        );
        return;
      case ConversationApiErrorKind.serviceBusy:
        _fail(
          'Aanya is finishing another response. Try again in a moment.',
          category: 'service_busy',
        );
        return;
      case ConversationApiErrorKind.unavailable:
        _fail(
          backendUnavailableMessage,
          connection: LocalAiConnection.offline,
          category: 'backend_unavailable',
        );
        return;
      case ConversationApiErrorKind.timeout:
        _fail(
          'Aanya took too long to respond. Please record a new turn to try '
          'again.',
          category: 'timeout',
        );
        return;
      case ConversationApiErrorKind.processingFailure:
        _fail(
          "The local AI couldn't finish that response. Please try again.",
          category: 'processing_failure',
        );
        return;
      case ConversationApiErrorKind.unexpectedResponse:
        _fail(
          'Aira received an unexpected response from the local AI service.',
          category: 'unexpected_response',
        );
        return;
    }
  }

  void _fail(
    String message, {
    LocalAiConnection? connection,
    required String category,
  }) {
    if (_disposed) return;
    _debugLog('error: $category');
    _emit(
      _state.copyWith(
        phase: VoiceCallPhase.error,
        connection: connection,
        statusLabel: 'Ready to try again',
        userMessage: message,
      ),
    );
  }

  bool _isCurrent(_RecordingAttempt attempt) =>
      !_disposed &&
      !attempt.cancelRequested &&
      identical(_recordingAttempt, attempt) &&
      attempt.token == _operationGeneration;

  bool _isOperationCurrent(int token) =>
      !_disposed && token == _operationGeneration;

  Future<void> _deleteAttemptPath(_RecordingAttempt attempt) async {
    final path = attempt.path;
    if (path == null || attempt.pathDeleted) return;
    attempt.pathDeleted = true;
    await _safeDelete(path);
  }

  Future<void> _safeDelete(String path) async {
    try {
      await recordingStore.delete(path);
    } on Object {
      _debugLog('temporary recording cleanup failed');
    }
  }

  Future<void> _safeRecorderCancel() async {
    try {
      await recorder.cancel();
    } on Object {
      _debugLog('recorder cancellation failed');
    }
  }

  Future<void> _safePlaybackStop() async {
    try {
      await playback.stop();
    } on Object {
      _debugLog('audio stop failed');
    }
  }

  void _emit(VoiceCallState nextState) {
    if (_disposed) return;
    final previousPhase = _state.phase;
    _state = nextState;
    _debugLog('state: ${previousPhase.name} -> ${nextState.phase.name}');
    notifyListeners();
  }

  void _logLatency({
    required DateTime recordingStoppedAt,
    required DateTime responseReceivedAt,
    required DateTime playbackStartedAt,
  }) {
    _debugLog(
      'latency: json_to_audio_start_ms='
      '${playbackStartedAt.difference(responseReceivedAt).inMilliseconds}, '
      'release_to_first_audio_ms='
      '${playbackStartedAt.difference(recordingStoppedAt).inMilliseconds}',
    );
  }

  void _debugLog(String message) {
    if (kDebugMode) debugPrint('Aanya voice: $message');
  }

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _operationGeneration += 1;
    final attempt = _recordingAttempt;
    if (attempt != null) attempt.cancelRequested = true;
    _state = _state.copyWith(
      phase: VoiceCallPhase.disposed,
      statusLabel: 'Voice session closed',
      clearUserMessage: true,
    );
    _shutdownComplete = _shutdown(attempt);
    super.dispose();
  }

  Future<void> _shutdown(_RecordingAttempt? attempt) async {
    api.dispose();
    if (attempt != null && attempt.started) await _safeRecorderCancel();
    if (attempt != null) await _deleteAttemptPath(attempt);
    await _safePlaybackStop();
    try {
      await recorder.dispose();
    } on Object {
      _debugLog('recorder disposal failed');
    }
    try {
      await playback.dispose();
    } on Object {
      _debugLog('audio disposal failed');
    }
    try {
      await recordingStore.dispose();
    } on Object {
      _debugLog('temporary recording cleanup failed');
    }
  }
}

final class _RecordingAttempt {
  _RecordingAttempt(this.token);

  final int token;
  String? path;
  DateTime? startedAt;
  bool started = false;
  bool finishRequested = false;
  bool finishing = false;
  bool cancelRequested = false;
  bool pathDeleted = false;
  Future<void>? cancellation;
}
