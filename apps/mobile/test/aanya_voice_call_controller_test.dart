import 'dart:async';

import 'package:female_voice_ai/features/call/application/aanya_voice_call_controller.dart';
import 'package:female_voice_ai/features/call/application/voice_io.dart';
import 'package:female_voice_ai/features/call/data/conversation_api.dart';
import 'package:female_voice_ai/features/call/domain/conversation_turn_response.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AanyaVoiceCallController', () {
    test('supports only Aanya and moves checkingConnection to idle', () async {
      expect(AanyaVoiceCallController.supportsCompanion('aanya'), isTrue);
      expect(AanyaVoiceCallController.supportsCompanion('tara'), isFalse);
      expect(
        () => AanyaVoiceCallController(
          companionId: 'tara',
          api: _FakeConversationApi(),
          recorder: _FakeVoiceRecorder(),
          playback: _FakeVoicePlayback(),
          recordingStore: _FakeRecordingStore(),
        ),
        throwsArgumentError,
      );

      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      final healthResult = Completer<bool>();
      fixture.api.checkHealthAction = () => healthResult.future;
      final phases = <VoiceCallPhase>[];
      fixture.controller.addListener(() {
        phases.add(fixture.controller.state.phase);
      });

      final initialization = fixture.controller.initialize();

      expect(
        fixture.controller.state.phase,
        VoiceCallPhase.checkingConnection,
      );
      expect(
        fixture.controller.state.connection,
        LocalAiConnection.checking,
      );
      healthResult.complete(true);
      await initialization;

      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(
        fixture.controller.state.connection,
        LocalAiConnection.connected,
      );
      expect(fixture.controller.state.canStartRecording, isTrue);
      expect(
        phases,
        <VoiceCallPhase>[
          VoiceCallPhase.checkingConnection,
          VoiceCallPhase.idle,
        ],
      );

      await fixture.controller.initialize();
      expect(fixture.api.checkHealthCallCount, 1);
    });

    test(
      'runs recording -> processing -> playing -> idle and appends the turn',
      () async {
        final fixture = _ControllerFixture();
        addTearDown(fixture.close);
        final responsePending = Completer<ConversationTurnResponse>();
        final playbackPending = Completer<void>();
        fixture.api.createTurnAction = (_) => responsePending.future;
        fixture.playback.playAction = (audioUri, onPlaybackStarted) {
          onPlaybackStarted();
          return playbackPending.future;
        };
        final phases = <VoiceCallPhase>[];
        fixture.controller.addListener(() {
          phases.add(fixture.controller.state.phase);
        });

        await _connect(fixture);
        expect(await fixture.controller.beginRecording(), isTrue);

        expect(fixture.controller.state.phase, VoiceCallPhase.recording);
        expect(fixture.controller.state.statusLabel, contains('Listening'));
        expect(fixture.recorder.startCallCount, 1);
        expect(fixture.playback.stopCallCount, 1);

        final finish = fixture.controller.finishRecording();
        await _drainAsyncWork();

        expect(fixture.controller.state.phase, VoiceCallPhase.processing);
        expect(fixture.controller.state.statusLabel, contains('Understanding'));
        expect(fixture.api.createTurnCallCount, 1);
        expect(fixture.api.submittedPaths, <String>[fixture.store.paths.single]);

        final response = _response();
        responsePending.complete(response);
        await _drainAsyncWork();

        expect(fixture.controller.state.phase, VoiceCallPhase.playing);
        expect(fixture.controller.state.statusLabel, contains('speaking'));
        expect(fixture.playback.playedUris, <Uri>[response.audioUri]);
        expect(fixture.controller.state.turns, hasLength(1));
        expect(
          fixture.controller.state.turns.single.userTranscript,
          response.normalizedTranscript,
        );
        expect(
          fixture.controller.state.turns.single.assistantResponse,
          response.response,
        );
        expect(
          fixture.controller.state.turns.single.aiDisclosure,
          contains('AI'),
        );

        playbackPending.complete();
        await finish;

        expect(fixture.controller.state.phase, VoiceCallPhase.idle);
        expect(fixture.controller.state.canStartRecording, isTrue);
        expect(fixture.store.deletedPaths, <String>[fixture.store.paths.single]);
        expect(
          phases,
          containsAllInOrder(<VoiceCallPhase>[
            VoiceCallPhase.checkingConnection,
            VoiceCallPhase.idle,
            VoiceCallPhase.recording,
            VoiceCallPhase.processing,
            VoiceCallPhase.playing,
            VoiceCallPhase.idle,
          ]),
        );
      },
    );

    test('permission denial is recoverable and never records or posts', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      fixture.recorder.permissionGranted = false;
      await _connect(fixture);

      expect(await fixture.controller.beginRecording(), isFalse);

      expect(fixture.controller.state.phase, VoiceCallPhase.error);
      expect(fixture.controller.state.connection, LocalAiConnection.connected);
      expect(
        fixture.controller.state.userMessage,
        contains('Microphone permission'),
      );
      expect(fixture.controller.state.canStartRecording, isTrue);
      expect(fixture.recorder.startCallCount, 0);
      expect(fixture.store.allocateCallCount, 0);
      expect(fixture.api.createTurnCallCount, 0);

      fixture.controller.clearRecoverableError();
      fixture.recorder.permissionGranted = true;
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(await fixture.controller.beginRecording(), isTrue);
      await fixture.controller.cancelRecording();
    });

    test('unavailable backend requires a successful health retry', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      fixture.api.createTurnAction = (_) => Future<ConversationTurnResponse>.error(
        const ConversationApiException(ConversationApiErrorKind.unavailable),
      );
      await _connect(fixture);
      await _recordOneTurn(fixture);

      expect(fixture.controller.state.phase, VoiceCallPhase.error);
      expect(fixture.controller.state.connection, LocalAiConnection.offline);
      expect(fixture.controller.state.userMessage, contains("can't reach"));
      expect(fixture.controller.state.canStartRecording, isFalse);
      expect(fixture.controller.state.canRetryConnection, isTrue);
      expect(fixture.store.deletedPaths, <String>[fixture.store.paths.single]);

      fixture.api.healthResult = true;
      await fixture.controller.retryConnection();

      expect(fixture.api.checkHealthCallCount, 2);
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(
        fixture.controller.state.connection,
        LocalAiConnection.connected,
      );
      expect(fixture.controller.state.userMessage, isNull);
    });

    for (final errorCase in <_ApiErrorCase>[
      const _ApiErrorCase(
        ConversationApiErrorKind.processingFailure,
        "couldn't finish",
      ),
      const _ApiErrorCase(
        ConversationApiErrorKind.serviceBusy,
        'finishing another response',
      ),
    ]) {
      test('${errorCase.kind.name} is recoverable without a retry POST', () async {
        final fixture = _ControllerFixture();
        addTearDown(fixture.close);
        fixture.api.createTurnAction = (_) =>
            Future<ConversationTurnResponse>.error(
              ConversationApiException(errorCase.kind),
            );
        await _connect(fixture);
        await _recordOneTurn(fixture);

        expect(fixture.controller.state.phase, VoiceCallPhase.error);
        expect(
          fixture.controller.state.connection,
          LocalAiConnection.connected,
        );
        expect(
          fixture.controller.state.userMessage,
          contains(errorCase.messageFragment),
        );
        expect(fixture.controller.state.canStartRecording, isTrue);
        expect(fixture.api.createTurnCallCount, 1);
        expect(fixture.playback.playCallCount, 0);
        expect(fixture.store.deletedPaths, <String>[fixture.store.paths.single]);

        fixture.controller.clearRecoverableError();
        expect(fixture.controller.state.phase, VoiceCallPhase.idle);
        expect(fixture.api.createTurnCallCount, 1);
      });
    }

    test('playback failure keeps the turn and permits a later turn', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      var responseNumber = 0;
      fixture.api.createTurnAction = (_) async {
        responseNumber += 1;
        return _response(turnId: 'turn_$responseNumber');
      };
      fixture.playback.playAction = (_, onPlaybackStarted) async {
        onPlaybackStarted();
        throw StateError('fake playback failure');
      };
      await _connect(fixture);
      await _recordOneTurn(fixture);

      expect(fixture.controller.state.phase, VoiceCallPhase.error);
      expect(fixture.controller.state.userMessage, contains("couldn't play"));
      expect(fixture.controller.state.turns, hasLength(1));
      expect(fixture.controller.state.canStartRecording, isTrue);

      fixture.playback.playAction = null;
      expect(await fixture.controller.beginRecording(), isTrue);
      await fixture.controller.finishRecording();

      expect(fixture.api.createTurnCallCount, 2);
      expect(fixture.playback.playCallCount, 2);
      expect(fixture.controller.state.turns, hasLength(2));
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(fixture.controller.state.userMessage, isNull);
    });

    test('double begin and double finish submit one recording once', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      final responsePending = Completer<ConversationTurnResponse>();
      fixture.api.createTurnAction = (_) => responsePending.future;
      await _connect(fixture);

      expect(await fixture.controller.beginRecording(), isTrue);
      expect(await fixture.controller.beginRecording(), isFalse);
      expect(fixture.recorder.startCallCount, 1);

      final firstFinish = fixture.controller.finishRecording();
      final duplicateFinish = fixture.controller.finishRecording();
      await _drainAsyncWork();

      expect(fixture.controller.state.phase, VoiceCallPhase.processing);
      expect(fixture.recorder.stopCallCount, 1);
      expect(fixture.api.createTurnCallCount, 1);
      expect(await fixture.controller.beginRecording(), isFalse);

      responsePending.complete(_response());
      await Future.wait(<Future<void>>[firstFinish, duplicateFinish]);

      expect(fixture.api.createTurnCallCount, 1);
      expect(fixture.playback.playCallCount, 1);
      expect(fixture.controller.state.turns, hasLength(1));
    });

    test('microphone is blocked while a request is processing', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      final responsePending = Completer<ConversationTurnResponse>();
      fixture.api.createTurnAction = (_) => responsePending.future;
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);

      final finish = fixture.controller.finishRecording();
      await _drainAsyncWork();
      expect(fixture.controller.state.phase, VoiceCallPhase.processing);

      expect(await fixture.controller.beginRecording(), isFalse);
      expect(fixture.recorder.startCallCount, 1);
      expect(fixture.api.createTurnCallCount, 1);

      responsePending.complete(_response());
      await finish;
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
    });

    test('microphone is blocked while response audio is playing', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      final playbackPending = Completer<void>();
      fixture.playback.playAction = (_, onPlaybackStarted) {
        onPlaybackStarted();
        return playbackPending.future;
      };
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);

      final finish = fixture.controller.finishRecording();
      await _drainAsyncWork();
      expect(fixture.controller.state.phase, VoiceCallPhase.playing);

      expect(await fixture.controller.beginRecording(), isFalse);
      expect(fixture.recorder.startCallCount, 1);
      expect(fixture.api.createTurnCallCount, 1);

      playbackPending.complete();
      await finish;
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
    });

    test('too-short recording is deleted and never submitted', () async {
      final instant = DateTime.utc(2026, 9, 2, 12);
      final fixture = _ControllerFixture(now: () => instant);
      addTearDown(fixture.close);
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);

      await fixture.controller.finishRecording();

      expect(fixture.controller.state.phase, VoiceCallPhase.error);
      expect(fixture.controller.state.userMessage, contains('too short'));
      expect(fixture.controller.state.canStartRecording, isTrue);
      expect(fixture.recorder.stopCallCount, 1);
      expect(fixture.api.createTurnCallCount, 0);
      expect(fixture.playback.playCallCount, 0);
      expect(fixture.store.deletedPaths, <String>[fixture.store.paths.single]);
    });

    test('backgrounding an active recording cancels and cleans it', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);
      final path = fixture.store.paths.single;

      await fixture.controller.handleAppBackgrounded();

      expect(fixture.recorder.cancelCallCount, 1);
      expect(fixture.store.deletedPaths, <String>[path]);
      expect(fixture.api.createTurnCallCount, 0);
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(
        fixture.controller.state.connection,
        LocalAiConnection.connected,
      );

      fixture.controller.handleAppResumed();
      expect(await fixture.controller.beginRecording(), isTrue);
      await fixture.controller.cancelRecording();
      expect(fixture.recorder.startCallCount, 2);
    });

    test('backgrounding playback stops it and suppresses stale completion', () async {
      final fixture = _ControllerFixture();
      addTearDown(fixture.close);
      final playbackPending = Completer<void>();
      fixture.playback.playAction = (_, onPlaybackStarted) {
        onPlaybackStarted();
        return playbackPending.future;
      };
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);
      final finish = fixture.controller.finishRecording();
      await _drainAsyncWork();
      expect(fixture.controller.state.phase, VoiceCallPhase.playing);

      await fixture.controller.handleAppBackgrounded();

      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(fixture.playback.stopCallCount, 2);
      playbackPending.complete();
      await finish;
      expect(fixture.controller.state.phase, VoiceCallPhase.idle);
      expect(fixture.controller.state.turns, hasLength(1));
    });

    test('dispose cancels recording and disposes every owned boundary', () async {
      final fixture = _ControllerFixture();
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);
      final path = fixture.store.paths.single;

      fixture.controller.dispose();
      expect(fixture.controller.state.phase, VoiceCallPhase.disposed);
      await fixture.controller.shutdownComplete;

      expect(fixture.api.disposed, isTrue);
      expect(fixture.recorder.cancelCallCount, 1);
      expect(fixture.recorder.disposeCallCount, 1);
      expect(fixture.playback.stopCallCount, 2);
      expect(fixture.playback.disposeCallCount, 1);
      expect(fixture.store.deletedPaths, <String>[path]);
      expect(fixture.store.disposeCallCount, 1);

      fixture.controller.dispose();
      await fixture.controller.shutdownComplete;
      expect(fixture.recorder.disposeCallCount, 1);
      expect(fixture.playback.disposeCallCount, 1);
    });

    test('late API result after dispose cannot emit or start playback', () async {
      final fixture = _ControllerFixture();
      final responsePending = Completer<ConversationTurnResponse>();
      fixture.api.createTurnAction = (_) => responsePending.future;
      var notificationCount = 0;
      fixture.controller.addListener(() {
        notificationCount += 1;
      });
      await _connect(fixture);
      expect(await fixture.controller.beginRecording(), isTrue);
      final path = fixture.store.paths.single;
      final finish = fixture.controller.finishRecording();
      await _drainAsyncWork();
      expect(fixture.api.createTurnCallCount, 1);
      final countBeforeDispose = notificationCount;

      fixture.controller.dispose();
      await fixture.controller.shutdownComplete;
      expect(fixture.controller.state.phase, VoiceCallPhase.disposed);

      responsePending.complete(_response());
      await finish;

      expect(notificationCount, countBeforeDispose);
      expect(fixture.controller.state.phase, VoiceCallPhase.disposed);
      expect(fixture.controller.state.turns, isEmpty);
      expect(fixture.playback.playCallCount, 0);
      expect(fixture.store.deletedPaths, contains(path));
    });
  });
}

Future<void> _connect(_ControllerFixture fixture) async {
  await fixture.controller.initialize();
  expect(fixture.controller.state.phase, VoiceCallPhase.idle);
  expect(fixture.controller.state.connection, LocalAiConnection.connected);
}

Future<void> _recordOneTurn(_ControllerFixture fixture) async {
  expect(await fixture.controller.beginRecording(), isTrue);
  await fixture.controller.finishRecording();
}

Future<void> _drainAsyncWork() async {
  for (var iteration = 0; iteration < 3; iteration += 1) {
    await Future<void>.delayed(Duration.zero);
  }
}

ConversationTurnResponse _response({String turnId = 'turn_1'}) {
  return ConversationTurnResponse(
    aiDisclosure: 'Aanya is a fictional AI companion.',
    turnId: turnId,
    companion: 'aanya',
    rawTranscript: 'Hello Anya',
    normalizedTranscript: 'Hello Aanya',
    response: "Hello. I'm Aanya, an AI companion.",
    audioUrl: '/v1/conversation/turns/$turnId/audio',
    audioUri: Uri.parse(
      'http://192.0.2.1:8765/v1/conversation/turns/$turnId/audio',
    ),
  );
}

final class _ControllerFixture {
  _ControllerFixture({DateTime Function()? now}) {
    final clock = _SteppingClock();
    controller = AanyaVoiceCallController(
      companionId: 'aanya',
      api: api,
      recorder: recorder,
      playback: playback,
      recordingStore: store,
      now: now ?? clock.next,
    );
  }

  final _FakeConversationApi api = _FakeConversationApi();
  final _FakeVoiceRecorder recorder = _FakeVoiceRecorder();
  final _FakeVoicePlayback playback = _FakeVoicePlayback();
  final _FakeRecordingStore store = _FakeRecordingStore();
  late final AanyaVoiceCallController controller;

  Future<void> close() async {
    controller.dispose();
    await controller.shutdownComplete;
  }
}

final class _ApiErrorCase {
  const _ApiErrorCase(this.kind, this.messageFragment);

  final ConversationApiErrorKind kind;
  final String messageFragment;
}

final class _SteppingClock {
  DateTime _next = DateTime.utc(2026, 9, 2, 12);

  DateTime next() {
    final value = _next;
    _next = _next.add(const Duration(seconds: 1));
    return value;
  }
}

final class _FakeConversationApi implements ConversationApi {
  bool healthResult = true;
  Future<bool> Function()? checkHealthAction;
  Future<ConversationTurnResponse> Function(String audioPath)? createTurnAction;
  int checkHealthCallCount = 0;
  int createTurnCallCount = 0;
  final List<String> submittedPaths = <String>[];
  bool disposed = false;

  @override
  Future<bool> checkHealth() {
    checkHealthCallCount += 1;
    final action = checkHealthAction;
    return action == null ? Future<bool>.value(healthResult) : action();
  }

  @override
  Future<ConversationTurnResponse> createAanyaTurn(String audioPath) {
    createTurnCallCount += 1;
    submittedPaths.add(audioPath);
    final action = createTurnAction;
    return action == null
        ? Future<ConversationTurnResponse>.value(_response())
        : action(audioPath);
  }

  @override
  void dispose() {
    disposed = true;
  }
}

final class _FakeVoiceRecorder implements VoiceRecorder {
  bool permissionGranted = true;
  Future<bool> Function()? permissionAction;
  Future<void> Function(String outputPath)? startAction;
  Future<String?> Function()? stopAction;
  Future<void> Function()? cancelAction;
  Future<void> Function()? disposeAction;
  int permissionCallCount = 0;
  int startCallCount = 0;
  int stopCallCount = 0;
  int cancelCallCount = 0;
  int disposeCallCount = 0;
  String? activePath;

  @override
  Future<bool> hasPermission() {
    permissionCallCount += 1;
    final action = permissionAction;
    return action == null ? Future<bool>.value(permissionGranted) : action();
  }

  @override
  Future<void> startWav(String outputPath) {
    startCallCount += 1;
    activePath = outputPath;
    final action = startAction;
    return action == null ? Future<void>.value() : action(outputPath);
  }

  @override
  Future<String?> stop() {
    stopCallCount += 1;
    final action = stopAction;
    return action == null ? Future<String?>.value(activePath) : action();
  }

  @override
  Future<void> cancel() {
    cancelCallCount += 1;
    final action = cancelAction;
    return action == null ? Future<void>.value() : action();
  }

  @override
  Future<void> dispose() {
    disposeCallCount += 1;
    final action = disposeAction;
    return action == null ? Future<void>.value() : action();
  }
}

final class _FakeVoicePlayback implements VoicePlayback {
  Future<void> Function(Uri audioUri, void Function() onPlaybackStarted)?
  playAction;
  Future<void> Function()? stopAction;
  Future<void> Function()? disposeAction;
  int playCallCount = 0;
  int stopCallCount = 0;
  int disposeCallCount = 0;
  final List<Uri> playedUris = <Uri>[];

  @override
  Future<void> play(
    Uri audioUri, {
    required void Function() onPlaybackStarted,
  }) {
    playCallCount += 1;
    playedUris.add(audioUri);
    final action = playAction;
    if (action != null) return action(audioUri, onPlaybackStarted);
    onPlaybackStarted();
    return Future<void>.value();
  }

  @override
  Future<void> stop() {
    stopCallCount += 1;
    final action = stopAction;
    return action == null ? Future<void>.value() : action();
  }

  @override
  Future<void> dispose() {
    disposeCallCount += 1;
    final action = disposeAction;
    return action == null ? Future<void>.value() : action();
  }
}

final class _FakeRecordingStore implements TemporaryRecordingStore {
  int allocateCallCount = 0;
  int disposeCallCount = 0;
  final List<String> paths = <String>[];
  final List<String> deletedPaths = <String>[];

  @override
  Future<String> allocateWavPath() {
    allocateCallCount += 1;
    final path = '/virtual/aira/recording_$allocateCallCount.wav';
    paths.add(path);
    return Future<String>.value(path);
  }

  @override
  Future<void> delete(String path) {
    deletedPaths.add(path);
    return Future<void>.value();
  }

  @override
  Future<void> dispose() {
    disposeCallCount += 1;
    return Future<void>.value();
  }
}
