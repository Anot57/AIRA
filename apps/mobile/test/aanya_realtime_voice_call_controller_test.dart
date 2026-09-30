import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:female_voice_ai/core/config/aira_api_config.dart';
import 'package:female_voice_ai/features/call/application/aanya_realtime_client.dart';
import 'package:female_voice_ai/features/call/application/aanya_realtime_voice_call_controller.dart';
import 'package:female_voice_ai/features/call/application/aanya_voice_call_controller.dart';
import 'package:female_voice_ai/features/call/application/voice_io.dart';
import 'package:female_voice_ai/features/call/data/realtime_web_socket.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('microphone framer emits bounded even 40 ms PCM16 frames', () {
    final framer = Pcm16FrameFramer();
    expect(framer.add(Uint8List(639)), isEmpty);
    final frames = framer.add(Uint8List(2001));
    expect(frames.map((frame) => frame.length), <int>[1280, 1280]);
    expect(frames, everyElement(predicate<Uint8List>((f) => f.length.isEven)));
    expect(framer.flush()?.length, 80);
  });

  test('microphone framer rejects an incomplete final PCM16 sample', () {
    final framer = Pcm16FrameFramer()..add(Uint8List(3));
    expect(framer.flush, throwsFormatException);
  });

  test('screen entry preconnects one WebSocket before Start Call', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();

    expect(harness.connector.connectCount, 1);
    expect(_sentTypes(harness.socket), <String>['session_start']);
    expect(harness.platform.startCount, 0);
    expect(harness.recorder.startCount, 0);
    expect(harness.recorder.prepareCount, 1);
    expect(harness.playback.prepareCount, 1);

    await harness.startCall();
    expect(harness.platform.startCount, 1);
    // Safe audio plumbing is reused rather than lazily repeated on turn one.
    expect(harness.recorder.prepareCount, 1);
    expect(harness.playback.prepareCount, 1);
    expect(harness.recorder.startCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
  });

  test('warming backend keeps accepted call connecting without opening mic', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize(ready: false);

    expect(await harness.controller.startCall(), isTrue);
    await _flush();
    expect(harness.controller.state.callActive, isTrue);
    expect(harness.controller.state.phase, VoiceCallPhase.connecting);
    expect(harness.recorder.startCount, 0);
  });

  test('30 seconds of silence produces zero turns and zero network PCM', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    for (var frame = 0; frame < 750; frame += 1) {
      harness.addPcm(_quietPcm, advance: const Duration(milliseconds: 40));
    }
    await _flush();

    expect(harness.socket.sentBinary, isEmpty);
    expect(_sentTypes(harness.socket), isNot(contains('end_of_turn')));
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.stopCount, 0);
  });

  test('400 ms pre-speech ring is prepended exactly once and in order', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    final leadingSilence = <Uint8List>[
      for (var amplitude = 1; amplitude <= 12; amplitude += 1)
        _pcm(amplitude: amplitude),
    ];
    final onset = <Uint8List>[
      _pcm(amplitude: 1000),
      _pcm(amplitude: 1300),
      _pcm(amplitude: 900),
    ];
    for (final frame in <Uint8List>[...leadingSilence, ...onset]) {
      harness.addPcm(frame, advance: const Duration(milliseconds: 40));
    }
    await _flush(20);

    final expected = <Uint8List>[...leadingSilence.skip(5), ...onset];
    expect(harness.socket.sentBinary, hasLength(10));
    for (var index = 0; index < expected.length; index += 1) {
      expect(harness.socket.sentBinary[index], expected[index], reason: 'frame $index');
    }
  });

  test('only 200 ms trailing padding reaches STT after the last voice', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    await harness.endpointUserSpeech();

    // Three onset frames plus five 40 ms trailing frames. The remaining
    // 2.8 seconds used to confirm the endpoint stay local to the VAD.
    expect(harness.socket.sentBinary, hasLength(8));
    expect(harness.socket.sentBinary.take(3), everyElement(isNot(_quietPcm)));
    expect(harness.socket.sentBinary.skip(3), everyElement(_quietPcm));
  });

  final shortSpeechProfiles = <(String, int, int, int)>[
    ('Hi Aanya', 5000, 5, 1000),
    ('Hey Aanya', 5000, 6, 1000),
    ('Hello Aanya', 5000, 7, 2000),
    ('Yes', 5000, 3, 1000),
    ('No', 5000, 3, 1000),
    ('Okay', 5000, 4, 1000),
    ('short quiet utterance', 700, 4, 1000),
    ('normal sentence', 5000, 20, 1000),
    ('fast sentence', 5000, 10, 1000),
  ];
  for (final profile in shortSpeechProfiles) {
    test('${profile.$1} profile preserves onset and creates one turn', () async {
      final harness = _Harness();
      addTearDown(harness.dispose);
      await harness.initialize();
      await harness.startCall();

      final initialSilenceFrames = profile.$4 ~/ 40;
      for (var frame = 0; frame < initialSilenceFrames; frame += 1) {
        harness.addPcm(_quietPcm, advance: const Duration(milliseconds: 40));
      }
      for (var frame = 0; frame < profile.$3; frame += 1) {
        final modulation = switch (frame % 3) {
          0 => 1.0,
          1 => 1.2,
          _ => 1.05,
        };
        harness.addPcm(
          _pcm(amplitude: (profile.$2 * modulation).round()),
          advance: const Duration(milliseconds: 40),
        );
      }
      await harness.silenceUntilEndpoint();

      expect(harness.socket.sentBinary, isNotEmpty);
      expect(
        _sentTypes(harness.socket).where((type) => type == 'end_of_turn'),
        hasLength(1),
      );
      expect(harness.connector.connectCount, 1);
    });
  }

  test('3000 ms post-speech silence sends end_of_turn exactly once', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    await harness.endpointUserSpeech();

    expect(harness.socket.sentBinary, isNotEmpty);
    expect(
      _sentTypes(
        harness.socket,
      ).where((type) => type == 'end_of_turn'),
      hasLength(1),
    );
    harness.addPcm(_quietPcm, advance: const Duration(seconds: 1));
    await _flush();
    expect(
      _sentTypes(
        harness.socket,
      ).where((type) => type == 'end_of_turn'),
      hasLength(1),
    );
  });

  test('2999 ms natural pause resumes the same server turn', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    harness.confirmSpeech();
    harness.addPcm(_quietPcm, advance: const Duration(milliseconds: 100));
    harness.addPcm(_voicePcm, advance: const Duration(milliseconds: 2899));
    await _flush();

    expect(_sentTypes(harness.socket), isNot(contains('end_of_turn')));
    expect(harness.connector.connectCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);

    await harness.silenceUntilEndpoint();
    expect(
      _sentTypes(
        harness.socket,
      ).where((type) => type == 'end_of_turn'),
      hasLength(1),
    );
  });

  test('AudioTrack completion is authoritative before listening resumes', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();

    final playbackGate = Completer<void>();
    harness.playback.finishGate = playbackGate;
    harness.completeServerTurn('turn-1');
    await _flush();

    expect(harness.controller.state.phase, VoiceCallPhase.speaking);
    expect(harness.recorder.startCount, 1);
    expect(harness.playback.finishCount, 1);

    playbackGate.complete();
    await _flush(10);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.startCount, 2);
    expect(harness.controller.state.turns, hasLength(1));
  });

  test('twenty automatic turns preserve one socket and sequence state', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    for (var turn = 1; turn <= 20; turn += 1) {
      await harness.endpointUserSpeech();
      harness.completeServerTurn('turn-$turn');
      await _flush(10);
      expect(
        harness.controller.state.phase,
        VoiceCallPhase.listening,
        reason: 'turn $turn',
      );
    }

    expect(harness.connector.connectCount, 1);
    expect(harness.controller.state.turns, hasLength(20));
    expect(
      _sentTypes(
        harness.socket,
      ).where((type) => type == 'end_of_turn'),
      hasLength(20),
    );
    expect(harness.recorder.startCount, 21);
  });

  test('20 background/resume cycles do not cancel, close, or duplicate', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    for (var cycle = 0; cycle < 20; cycle += 1) {
      await harness.controller.handleAppBackgrounded();
      harness.controller.handleAppResumed();
    }
    await _flush();

    expect(harness.socket.closeCount, 0);
    expect(harness.connector.connectCount, 1);
    expect(harness.recorder.cancelCount, 0);
    expect(harness.recorder.startCount, 1);
    expect(harness.platform.endCount, 0);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
  });

  test('background and volume/focus events preserve thinking and speaking', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket.sendJson(_turn('stt_final', text: 'Hello'));
    await _flush();
    expect(harness.controller.state.phase, VoiceCallPhase.thinking);

    await harness.controller.handleAppBackgrounded();
    harness.platform.emitFocus('loss_transient');
    harness.platform.emitFocus('gain');
    harness.controller.handleAppResumed();
    harness.socket
      ..sendJson(_turn('text_delta', delta: 'Hello there.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();
    expect(harness.controller.state.phase, VoiceCallPhase.speaking);

    await harness.controller.handleAppBackgrounded();
    harness.platform.emitFocus('loss_transient_can_duck');
    harness.platform.emitFocus('gain');
    await _flush();

    expect(harness.socket.closeCount, 0);
    expect(harness.platform.endCount, 0);
    expect(harness.playback.stopCount, 1); // initial listening setup only
    expect(_sentTypes(harness.socket), isNot(contains('cancel_turn')));
  });

  test('transient network loss reconnects same active call without ending', () async {
    final harness = _Harness(extraSockets: 1);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    await harness.socket.serverDisconnect();
    await _flush(10);
    expect(harness.connector.connectCount, 2);
    expect(harness.controller.state.phase, VoiceCallPhase.reconnecting);
    harness.sockets[1].sendJson(
      _sessionReady(sessionId: 'session-recovered'),
    );
    await _flush(10);

    expect(harness.platform.startCount, 1);
    expect(harness.platform.endCount, 0);
    expect(harness.controller.state.callActive, isTrue);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.startCount, 2);
  });

  test('bounded internal cancellation returns to automatic listening', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket.sendJson(_turn('stt_final', text: 'Hello'));
    await _flush();

    final cancellation = harness.controller.cancelActiveTurn();
    await _flush();
    expect(_sentTypes(harness.socket), contains('cancel_turn'));
    harness.socket.sendJson(_cancelled(turnId: 'turn-1'));
    await cancellation;
    await _flush();

    expect(harness.socket.closeCount, 0);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.startCount, 2);
  });

  test('End Call is exact-once and disables reconnect', () async {
    final harness = _Harness(extraSockets: 1);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    harness.confirmSpeech();

    await Future.wait(<Future<void>>[
      harness.controller.endCall(),
      harness.controller.endCall(),
    ]);
    await _flush();

    expect(harness.platform.endCount, 1);
    expect(harness.socket.closeCount, 1);
    expect(harness.recorder.cancelCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.ended);
    expect(harness.controller.state.callActive, isFalse);
    expect(_sentTypes(harness.socket), contains('session_end'));
    expect(harness.connector.connectCount, 1);
  });

  test('notification End Call ends Dart transport without echoing native end', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    harness.platform.emit(
      ActiveCallPlatformEventType.endRequested,
      reason: 'notification_end_call',
    );
    await _flush(10);

    expect(harness.platform.endCount, 0);
    expect(harness.socket.closeCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.ended);
  });

  test('ordered EOT waits for every accepted PCM socket write', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    final writeGate = Completer<void>();
    harness.socket.binaryWriteGate = writeGate;

    harness.confirmSpeech();
    await harness.silenceUntilEndpoint();
    expect(_sentTypes(harness.socket), isNot(contains('end_of_turn')));
    expect(harness.controller.audioAccounting.queuedForNetworkBytes, greaterThan(0));

    writeGate.complete();
    await _flush(20);
    expect(harness.socket.sentBinary, isNotEmpty);
    expect(_sentTypes(harness.socket), contains('end_of_turn'));
    expect(harness.controller.audioAccounting.queuedForNetworkBytes, 0);
  });

  test('dispose prevents stale native, socket, and focus callbacks', () async {
    final harness = _Harness();
    await harness.initialize();
    await harness.startCall();
    harness.controller.dispose();
    await harness.controller.shutdownComplete;

    harness.socket.sendJson(_turn('stt_final', text: 'stale'));
    harness.addPcm(_voicePcm, advance: const Duration(seconds: 4));
    harness.platform.emitFocus('gain');
    await _flush();

    expect(harness.controller.state.phase, VoiceCallPhase.disposed);
    expect(harness.playback.disposed, isTrue);
    expect(harness.socket.closeCount, 1);
    expect(harness.platform.endCount, 1);
  });

  test('Back during microphone start invalidates the pending start', () async {
    final harness = _Harness(resourceShutdownTimeout: const Duration(milliseconds: 20));
    addTearDown(harness.dispose);
    await harness.initialize();
    final startGate = Completer<void>();
    harness.recorder.startGate = startGate;

    final starting = harness.controller.startCall();
    await _waitFor(() => harness.recorder.startCount == 1);
    await harness.controller.endCall().timeout(const Duration(seconds: 1));
    startGate.complete();
    await starting;
    await _flush();

    expect(harness.controller.state.phase, VoiceCallPhase.ended);
    expect(harness.recorder.startCount, 1);
    expect(harness.recorder.cancelCount, 1);
  });

  test('Back is exact-once during processing and retrieval wait', () async {
    for (final label in <String>['processing', 'retrieval']) {
      final harness = _Harness();
      await harness.initialize();
      await harness.startCall();
      await harness.endpointUserSpeech();
      harness.socket.sendJson(_turn('stt_final', text: '$label request'));
      await _flush();
      expect(harness.controller.state.phase, VoiceCallPhase.thinking);

      await Future.wait(<Future<void>>[
        harness.controller.endCall(),
        harness.controller.endCall(),
      ]);

      expect(harness.controller.state.phase, VoiceCallPhase.ended);
      expect(harness.platform.endCount, 1);
      expect(harness.connector.connectCount, 1);
      await harness.dispose();
    }
  });

  test('Back during speaking stops playback and closes exactly once', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket
      ..sendJson(_turn('stt_final', text: 'Hello'))
      ..sendJson(_turn('text_delta', delta: 'Hello there.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();
    expect(harness.controller.state.phase, VoiceCallPhase.speaking);

    await harness.controller.endCall();

    expect(harness.controller.state.phase, VoiceCallPhase.ended);
    expect(harness.playback.stopCount, greaterThanOrEqualTo(2));
    expect(harness.socket.closeCount, 1);
    expect(harness.platform.endCount, 1);
  });

  test('intentional Back cancels a scheduled reconnect', () async {
    final reconnectGate = Completer<void>();
    final harness = _Harness(extraSockets: 1, reconnectGate: reconnectGate);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    await harness.socket.serverDisconnect();
    await _waitFor(
      () => harness.controller.state.phase == VoiceCallPhase.reconnecting,
    );
    await harness.controller.endCall();
    reconnectGate.complete();
    await _flush(12);

    expect(harness.connector.connectCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.ended);
  });

  test('call N callbacks cannot mutate a fresh call N plus 1', () async {
    final first = _Harness();
    await first.initialize();
    await first.startCall();
    final firstGeneration = first.recorder.callGeneration;
    await first.controller.endCall();

    final second = _Harness();
    await second.initialize();
    await second.startCall();
    final secondGeneration = second.recorder.callGeneration;

    first.socket.sendJson(_turn('stt_final', text: 'stale'));
    first.recorder.add(_voicePcm);
    first.platform.emitFocus('gain');
    await _flush();

    expect(secondGeneration, isNot(firstGeneration));
    expect(second.controller.state.phase, VoiceCallPhase.listening);
    expect(second.recorder.startCount, 1);
    await first.dispose();
    await second.dispose();
  });

  test('capture stall watchdog restarts microphone capture', () async {
    final harness = _Harness(
      captureStallTimeout: const Duration(milliseconds: 10),
    );
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();

    await _waitFor(() => harness.recorder.startCount >= 2);

    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.cancelCount, greaterThanOrEqualTo(1));
  });

  test('maximum speech watchdog submits a bounded turn', () async {
    final harness = _Harness(
      maximumSpeechDuration: const Duration(milliseconds: 10),
    );
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    harness.confirmSpeech();

    await _waitFor(() => _sentTypes(harness.socket).contains('end_of_turn'));

    expect(harness.recorder.stopCount, 1);
    expect(harness.controller.state.phase, VoiceCallPhase.finalizingUserTurn);
  });

  test('server progress watchdog cancels and resumes listening', () async {
    final harness = _Harness(
      serverProgressTimeout: const Duration(milliseconds: 10),
    );
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();

    await _waitFor(() => _sentTypes(harness.socket).contains('cancel_turn'));
    harness.socket.sendJson(_cancelled(turnId: 'turn-1'));
    await _waitFor(() => harness.recorder.startCount >= 2);

    expect(harness.controller.state.phase, VoiceCallPhase.listening);
  });

  test('failed microphone start recovers instead of deadlocking', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    harness.recorder.startFailures = 1;

    await harness.controller.startCall().timeout(const Duration(seconds: 1));
    await _waitFor(() => harness.recorder.startCount >= 2);
    await _flush();

    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    await harness.endpointUserSpeech();
  });

  test('interrupting while Aanya speaks stops her and resumes listening', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket
      ..sendJson(_turn('stt_final', text: 'Tell me a story'))
      ..sendJson(_turn('text_delta', delta: 'Once upon a time.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();
    expect(harness.controller.state.phase, VoiceCallPhase.speaking);
    final stopsBefore = harness.playback.stopCount;

    final interruption = harness.controller.cancelActiveTurn();
    await _flush();
    expect(_sentTypes(harness.socket), contains('cancel_turn'));
    expect(harness.playback.stopCount, greaterThan(stopsBefore));

    // Late output from the interrupted turn must never be played.
    final appendsBefore = harness.playback.appended.length;
    harness.socket
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[3, 0, 4, 0])
      ..sendJson(_cancelled(turnId: 'turn-1'));
    await interruption;
    await _flush();

    expect(harness.playback.appended.length, appendsBefore);
    expect(harness.socket.closeCount, 0);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.startCount, 2);
  });

  test('barge-in is off by default: the mic stays closed while Aanya speaks', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket
      ..sendJson(_turn('stt_final', text: 'Hello'))
      ..sendJson(_turn('text_delta', delta: 'Hello there.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();

    expect(harness.controller.state.phase, VoiceCallPhase.speaking);
    expect(harness.recorder.startCount, 1);
    expect(harness.recorder.echoCancellationRequests, <bool>[false]);
  });

  test('barge-in: talking over Aanya interrupts her and starts a new turn', () async {
    final harness = _Harness(bargeInEnabled: true);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    await _flush(12);
    // The monitor reopens the mic, with echo cancellation, once the turn is sent.
    expect(harness.recorder.startCount, 2);
    expect(harness.recorder.echoCancellationRequests, <bool>[true, true]);

    harness.socket
      ..sendJson(_turn('stt_final', text: 'Tell me a story'))
      ..sendJson(_turn('text_delta', delta: 'Once upon a time.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();
    expect(harness.controller.state.phase, VoiceCallPhase.speaking);
    final stopsBefore = harness.playback.stopCount;

    for (final amplitude in _bargeInSpeech) {
      harness.addPcm(
        _pcm(amplitude: amplitude),
        advance: const Duration(milliseconds: 40),
      );
    }
    await _flush();

    expect(_sentTypes(harness.socket), contains('cancel_turn'));
    expect(harness.playback.stopCount, greaterThan(stopsBefore));
    harness.socket.sendJson(_cancelled(turnId: 'turn-1'));
    await _flush(12);

    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(harness.recorder.startCount, 3);
    // The interrupting words carry into the new turn once speech is confirmed.
    final sentBefore = harness.socket.sentBinary.length;
    harness.confirmSpeech();
    await _flush();
    final newTurnFrames = harness.socket.sentBinary.skip(sentBefore).toList();
    expect(newTurnFrames, anyElement(equals(_pcm(amplitude: 6200))));
  });

  test('barge-in ignores quiet speaker leakage while Aanya speaks', () async {
    final harness = _Harness(bargeInEnabled: true);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    await _flush(12);
    harness.socket
      ..sendJson(_turn('stt_final', text: 'Tell me a story'))
      ..sendJson(_turn('text_delta', delta: 'Once upon a time.'))
      ..sendJson(_audioHeader())
      ..sendServerBinary(<int>[1, 0, 2, 0]);
    await _flush();

    for (var frame = 0; frame < 25; frame += 1) {
      harness.addPcm(
        _pcm(amplitude: frame.isEven ? 900 : 1100),
        advance: const Duration(milliseconds: 40),
      );
    }
    await _flush();

    expect(_sentTypes(harness.socket), isNot(contains('cancel_turn')));
    expect(harness.controller.state.phase, VoiceCallPhase.speaking);
  });

  test('barge-in monitor closes before listening resumes after a turn', () async {
    final harness = _Harness(bargeInEnabled: true);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    await _flush(12);
    final cancelsBefore = harness.recorder.cancelCount;

    harness.completeServerTurn('turn-1');
    await _flush(12);

    expect(harness.recorder.cancelCount, greaterThan(cancelsBefore));
    expect(harness.recorder.startCount, 3);
    expect(harness.controller.state.phase, VoiceCallPhase.listening);
    expect(_sentTypes(harness.socket), isNot(contains('cancel_turn')));
  });

  test('crisis escalation stays visible until dismissed or a new call', () async {
    final harness = _Harness();
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    expect(harness.controller.state.crisisResources, isEmpty);

    harness.socket
      ..sendJson(_turn('stt_final', text: 'main apni jaan le lunga'))
      ..sendJson(_safetyEscalation());
    await _flush();

    final resources = harness.controller.state.crisisResources;
    expect(resources.map((resource) => resource.phone), <String>['112', '14416']);

    harness.completeServerTurn('turn-1');
    await _flush(12);
    expect(harness.controller.state.crisisResources, hasLength(2));

    harness.controller.dismissCrisisResources();
    expect(harness.controller.state.crisisResources, isEmpty);
  });

  test('a new call never inherits the previous call crisis banner', () async {
    final harness = _Harness(extraSockets: 1);
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    await harness.endpointUserSpeech();
    harness.socket
      ..sendJson(_turn('stt_final', text: 'I want to die'))
      ..sendJson(_safetyEscalation());
    await _flush();
    expect(harness.controller.state.crisisResources, isNotEmpty);

    await harness.controller.endCall();
    await harness.controller.startCall();
    await _flush();

    expect(harness.controller.state.crisisResources, isEmpty);
  });

  test('Back cleanup remains bounded when platform resources hang', () async {
    final harness = _Harness(
      resourceShutdownTimeout: const Duration(milliseconds: 10),
    );
    addTearDown(harness.dispose);
    await harness.initialize();
    await harness.startCall();
    harness.recorder.cancelGate = Completer<void>();
    harness.platform.endGate = Completer<void>();

    await harness.controller.endCall().timeout(const Duration(seconds: 1));

    expect(harness.controller.state.phase, VoiceCallPhase.ended);
    expect(harness.socket.closeCount, 1);
  });
}

final class _MutableClock {
  Duration value = Duration.zero;
  Duration call() => value;
  void advance(Duration duration) => value += duration;
}

final class _Harness {
  _Harness({
    int extraSockets = 0,
    this.reconnectGate,
    Duration captureStallTimeout = const Duration(seconds: 30),
    Duration maximumSpeechDuration = const Duration(seconds: 30),
    Duration serverProgressTimeout = const Duration(seconds: 30),
    Duration resourceShutdownTimeout = const Duration(seconds: 3),
    bool bargeInEnabled = false,
  }) {
    sockets = List<_FakeSocket>.generate(
      extraSockets + 1,
      (_) => _FakeSocket(),
    );
    connector.sockets.addAll(sockets);
    client = AanyaRealtimeClient(
      companionId: 'aanya',
      config: AiraApiConfig.fromBaseUrl('http://127.0.0.1:8765'),
      connector: connector,
      handshakeTimeout: const Duration(seconds: 30),
      cancelAckTimeout: const Duration(milliseconds: 100),
      reconnectDelays: const <Duration>[Duration.zero],
      reconnectDelay: (_) => reconnectGate?.future ?? Future<void>.value(),
    );
    controller = AanyaRealtimeVoiceCallController(
      companionId: 'aanya',
      client: client,
      recorder: recorder,
      playback: playback,
      activeCallPlatform: platform,
      monotonicNow: clock.call,
      recoveryDelay: (_) async {},
      zeroAudioTimeout: const Duration(seconds: 30),
      captureStallTimeout: captureStallTimeout,
      maximumSpeechDuration: maximumSpeechDuration,
      serverProgressTimeout: serverProgressTimeout,
      resourceShutdownTimeout: resourceShutdownTimeout,
      bargeInEnabled: bargeInEnabled,
    );
  }

  final _MutableClock clock = _MutableClock();
  final _FakeConnector connector = _FakeConnector();
  final _FakeRecorder recorder = _FakeRecorder();
  final _FakePlayback playback = _FakePlayback();
  final _FakeActiveCallPlatform platform = _FakeActiveCallPlatform();
  final Completer<void>? reconnectGate;
  late final List<_FakeSocket> sockets;
  _FakeSocket get socket => sockets.first;
  late final AanyaRealtimeClient client;
  late final AanyaRealtimeVoiceCallController controller;

  Future<void> initialize({bool ready = true}) async {
    await controller.initialize();
    socket.sendJson(_sessionReady(ready: ready));
    await _flush();
  }

  Future<void> startCall() async {
    expect(await controller.startCall(), isTrue);
    await _flush();
    expect(controller.state.phase, VoiceCallPhase.listening);
  }

  void addPcm(Uint8List bytes, {required Duration advance}) {
    clock.advance(advance);
    recorder.add(bytes);
  }

  void confirmSpeech() {
    for (final amplitude in <int>[4200, 5800, 5000]) {
      addPcm(
        _pcm(amplitude: amplitude),
        advance: const Duration(milliseconds: 40),
      );
    }
  }

  Future<void> silenceUntilEndpoint() async {
    for (var frame = 0; frame < 75; frame += 1) {
      addPcm(_quietPcm, advance: const Duration(milliseconds: 40));
    }
    await _flush(12);
  }

  Future<void> endpointUserSpeech() async {
    confirmSpeech();
    await silenceUntilEndpoint();
    expect(_sentTypes(socket), contains('end_of_turn'));
  }

  void completeServerTurn(String turnId) {
    final target = connector.connectedSockets.last;
    target
      ..sendJson(_turn('stt_final', turnId: turnId, text: 'Hello Aanya'))
      ..sendJson(
        _turn('text_delta', turnId: turnId, delta: 'Hello there.'),
      )
      ..sendJson(_audioHeader(turnId: turnId))
      ..sendServerBinary(<int>[1, 0, 2, 0])
      ..sendJson(<String, Object?>{
        ..._turn('turn_complete', turnId: turnId),
        'metrics': <String, Object?>{'ttfa_ms': 900},
      });
  }

  Future<void> dispose() async {
    if (controller.state.phase != VoiceCallPhase.disposed) controller.dispose();
    await controller.shutdownComplete;
  }
}

final class _FakeRecorder
    implements
        RealtimeVoiceRecorder,
        CallGenerationAwareRealtimeVoiceRecorder {
  StreamController<Uint8List>? _stream;
  int prepareCount = 0;
  int startCount = 0;
  int stopCount = 0;
  int cancelCount = 0;
  bool disposed = false;
  int? callGeneration;
  Completer<void>? startGate;
  Completer<void>? cancelGate;
  int startFailures = 0;

  @override
  void bindCallGeneration(int generation) => callGeneration = generation;

  @override
  Future<bool> hasPermission() async => true;

  @override
  Future<void> prepare() async {
    prepareCount += 1;
  }

  final List<bool> echoCancellationRequests = <bool>[];

  @override
  Future<Stream<Uint8List>> startPcm16Stream({
    bool echoCancellation = false,
  }) async {
    startCount += 1;
    echoCancellationRequests.add(echoCancellation);
    if (startFailures > 0) {
      startFailures -= 1;
      throw const RealtimeMicrophoneException(
        'capture_busy',
        'Microphone capture is stopping.',
      );
    }
    _stream = StreamController<Uint8List>(sync: true);
    await startGate?.future;
    return _stream!.stream;
  }

  void add(Uint8List bytes) {
    if (!(_stream?.isClosed ?? true)) _stream!.add(Uint8List.fromList(bytes));
  }

  @override
  Future<void> stopStream() async {
    stopCount += 1;
    if (!(_stream?.isClosed ?? true)) await _stream!.close();
  }

  @override
  Future<void> cancel() async {
    cancelCount += 1;
    await cancelGate?.future;
    if (!(_stream?.isClosed ?? true)) await _stream!.close();
  }

  @override
  Future<void> dispose() async {
    disposed = true;
    if (!(_stream?.isClosed ?? true)) await _stream!.close();
  }
}

final class _FakePlayback implements RealtimeVoicePlayback {
  final List<int> startedRates = <int>[];
  final List<Uint8List> appended = <Uint8List>[];
  Completer<void>? finishGate;
  int prepareCount = 0;
  int stopCount = 0;
  int finishCount = 0;
  bool disposed = false;

  @override
  Future<void> prepare({int sampleRateHz = 24000}) async {
    prepareCount += 1;
  }

  @override
  Future<void> start({required int sampleRateHz}) async {
    startedRates.add(sampleRateHz);
  }

  @override
  Future<void> append(Uint8List bytes) async {
    appended.add(Uint8List.fromList(bytes));
  }

  @override
  Future<void> finish() async {
    finishCount += 1;
    await finishGate?.future;
    finishGate = null;
  }

  @override
  Future<void> stop() async {
    stopCount += 1;
  }

  @override
  Future<void> dispose() async {
    disposed = true;
  }
}

final class _FakeActiveCallPlatform implements ActiveCallPlatform {
  final StreamController<ActiveCallPlatformEvent> _events =
      StreamController<ActiveCallPlatformEvent>.broadcast(sync: true);
  int startCount = 0;
  int endCount = 0;
  int updateCount = 0;
  int generation = 0;
  Completer<void>? endGate;

  @override
  Stream<ActiveCallPlatformEvent> get events => _events.stream;

  @override
  Future<void> startCall({required int generation}) async {
    startCount += 1;
    this.generation = generation;
  }

  @override
  Future<void> updateState({
    required int generation,
    required String state,
    String? sessionId,
  }) async {
    updateCount += 1;
  }

  @override
  Future<void> endCall({required int generation}) async {
    endCount += 1;
    await endGate?.future;
  }

  void emitFocus(String focus) {
    emit(ActiveCallPlatformEventType.audioFocus, audioFocus: focus);
  }

  void emit(
    ActiveCallPlatformEventType type, {
    String? audioFocus,
    String? reason,
  }) {
    if (_events.isClosed) return;
    _events.add(
      ActiveCallPlatformEvent(
        type: type,
        generation: generation,
        monotonicNanos: 1,
        active: type != ActiveCallPlatformEventType.ended,
        audioFocus: audioFocus,
        reason: reason,
      ),
    );
  }

  @override
  Future<void> dispose() async {
    await _events.close();
  }
}

final class _FakeConnector implements RealtimeWebSocketConnector {
  final List<_FakeSocket> sockets = <_FakeSocket>[];
  final List<_FakeSocket> connectedSockets = <_FakeSocket>[];
  int connectCount = 0;

  @override
  Future<RealtimeWebSocket> connect(Uri uri) async {
    connectCount += 1;
    final socket = sockets.removeAt(0);
    connectedSockets.add(socket);
    return socket;
  }
}

final class _FakeSocket implements RealtimeWebSocket {
  final StreamController<Object?> _messages = StreamController<Object?>();
  final List<String> sentText = <String>[];
  final List<List<int>> sentBinary = <List<int>>[];
  Completer<void>? binaryWriteGate;
  int closeCount = 0;

  @override
  Stream<Object?> get messages => _messages.stream;

  @override
  void sendText(String text) => sentText.add(text);

  @override
  void sendBinary(Uint8List bytes) => sentBinary.add(List<int>.from(bytes));

  @override
  Future<void> sendBinaryComplete(Uint8List bytes) async {
    await binaryWriteGate?.future;
    sendBinary(bytes);
  }

  @override
  Future<void> sendTextComplete(String text) async => sendText(text);

  void sendJson(Map<String, Object?> value) {
    if (!_messages.isClosed) _messages.add(jsonEncode(value));
  }

  void sendServerBinary(List<int> bytes) {
    if (!_messages.isClosed) _messages.add(Uint8List.fromList(bytes));
  }

  Future<void> serverDisconnect() async {
    if (!_messages.isClosed) await _messages.close();
  }

  @override
  Future<void> close([int? code, String? reason]) async {
    closeCount += 1;
    if (!_messages.isClosed) await _messages.close();
  }
}

final Uint8List _quietPcm = _pcm(amplitude: 0);
// Loud, modulated speech held long enough for the stricter barge-in detector.
const List<int> _bargeInSpeech = <int>[
  4200, 5800, 5000, 6200, 4800, 5600, 5200, 6000, 4600, 5800, 5000, 6200,
];
final Uint8List _voicePcm = _pcm(amplitude: 5000);

Uint8List _pcm({required int amplitude, int samples = 640}) {
  final bytes = Uint8List(samples * 2);
  final data = ByteData.sublistView(bytes);
  for (var sample = 0; sample < samples; sample += 1) {
    data.setInt16(
      sample * 2,
      sample.isEven ? amplitude : -amplitude,
      Endian.little,
    );
  }
  return bytes;
}

Map<String, Object?> _sessionReady({
  bool ready = true,
  String sessionId = 'session-1',
}) => <String, Object?>{
  'type': 'session_ready',
  'protocol_version': 1,
  'session_id': sessionId,
  'companion': 'aanya',
  'ai_disclosure': 'Aanya is a fictional adult AI companion.',
  'readiness': <String, Object?>{
    'status': ready ? 'ready' : 'warming',
    'components': <String, Object?>{
      'stt': ready ? 'ready' : 'loading',
      'llm': ready ? 'ready' : 'loading',
      'tts': ready ? 'ready' : 'loading',
    },
    'message': ready ? 'Ready.' : 'Warming.',
  },
  'can_process_turns': ready,
  'audio_format': <String, Object?>{
    'encoding': 'pcm_s16le',
    'sample_rate_hz': 16000,
    'channels': 1,
  },
};

Map<String, Object?> _safetyEscalation({String turnId = 'turn-1'}) =>
    <String, Object?>{
      ..._turn('safety_escalation', turnId: turnId),
      'kind': 'crisis_resources',
      'resources': <Map<String, Object?>>[
        <String, Object?>{'label': 'Emergency services', 'phone': '112'},
        <String, Object?>{
          'label': 'Tele-MANAS mental health helpline',
          'phone': '14416',
        },
      ],
    };

Map<String, Object?> _turn(
  String type, {
  String turnId = 'turn-1',
  String? text,
  String? delta,
}) => <String, Object?>{
  'type': type,
  'protocol_version': 1,
  'turn_id': turnId,
  'text': ?text,
  'delta': ?delta,
};

Map<String, Object?> _audioHeader({String turnId = 'turn-1'}) =>
    <String, Object?>{
      ..._turn('audio_chunk', turnId: turnId),
      'sequence': 0,
      'encoding': 'pcm_s16le',
      'sample_rate_hz': 24000,
      'channels': 1,
      'byte_length': 4,
    };

Map<String, Object?> _cancelled({String? turnId}) => <String, Object?>{
  'type': 'recoverable_error',
  'protocol_version': 1,
  'turn_id': ?turnId,
  'code': 'turn_cancelled',
  'message': 'Cancelled.',
};

List<String> _sentTypes(_FakeSocket socket) => socket.sentText
    .map(
      (text) => (jsonDecode(text) as Map<String, dynamic>)['type']! as String,
    )
    .toList();

Future<void> _flush([int count = 6]) async {
  for (var i = 0; i < count; i += 1) {
    await Future<void>.delayed(Duration.zero);
  }
}

Future<void> _waitFor(bool Function() predicate) async {
  for (var attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await Future<void>.delayed(const Duration(milliseconds: 2));
  }
  fail('Condition did not become true.');
}
