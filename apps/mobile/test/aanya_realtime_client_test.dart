import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:female_voice_ai/core/config/aira_api_config.dart';
import 'package:female_voice_ai/features/call/application/aanya_realtime_client.dart';
import 'package:female_voice_ai/features/call/data/realtime_web_socket.dart';
import 'package:female_voice_ai/features/call/domain/realtime_voice_protocol.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('rejects every non-Aanya companion before opening a socket', () {
    expect(
      () => AanyaRealtimeClient(
        companionId: 'tara',
        config: _config,
        connector: _FakeConnector(),
      ),
      throwsArgumentError,
    );
  });

  test(
    'uses one connection and waits for session_ready before Ready',
    () async {
      final socket = _FakeSocket();
      final connector = _FakeConnector()..sockets.add(socket);
      final client = _client(connector);
      addTearDown(() async {
        client.dispose();
        await client.shutdownComplete;
      });

      await Future.wait(<Future<void>>[client.connect(), client.connect()]);

      expect(connector.connectCount, 1);
      expect(connector.uris, <Uri>[
        Uri.parse('ws://127.0.0.1:8765/v1/realtime'),
      ]);
      expect(client.state.phase, AanyaRealtimePhase.aiStarting);
      expect(client.state.isSessionReady, isFalse);
      expect(_sentTypes(socket), <String>['session_start']);

      socket.sendServerJson(_sessionReady());
      await _flushEvents();

      expect(client.state.phase, AanyaRealtimePhase.ready);
      expect(client.state.statusLabel, 'Ready');
      expect(client.state.sessionId, 'session-1');
      expect(
        client.state.aiDisclosure,
        contains('fictional adult AI companion'),
      );
    },
  );

  test('does not enable turns when transport reports warming models', () async {
    final socket = _FakeSocket();
    final client = _client(_FakeConnector()..sockets.add(socket));
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    await client.connect();

    socket.sendServerJson(<String, Object?>{
      ..._sessionReady(),
      'readiness': _readiness(status: 'warming', component: 'loading'),
      'can_process_turns': false,
    });
    await _flushEvents();

    expect(client.state.phase, AanyaRealtimePhase.aiStarting);
    expect(client.state.canStartTurn, isFalse);
    expect(client.state.statusLabel, 'AI warming up');
  });

  test('protects one active turn and sends raw bounded PCM frames', () async {
    final socket = _FakeSocket();
    final client = await _readyClient(socket);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });

    expect(client.beginTurn(), isTrue);
    expect(client.beginTurn(), isFalse);
    expect(client.state.phase, AanyaRealtimePhase.listening);
    expect(
      client.sendPcm16Chunk(Uint8List.fromList(<int>[1, 2, 3, 4])),
      isTrue,
    );
    expect(client.endTurn(), isTrue);
    expect(client.endTurn(), isFalse);

    expect(socket.sentBinary, hasLength(1));
    expect(socket.sentBinary.single, <int>[1, 2, 3, 4]);
    expect(_sentTypes(socket), <String>['session_start', 'end_of_turn']);
    expect(client.state.phase, AanyaRealtimePhase.finalizing);
    expect(client.state.statusLabel, 'Finishing your words');
  });

  test(
    'maps typed server events and pairs announced audio with binary',
    () async {
      final socket = _FakeSocket();
      final client = await _readyClient(socket);
      final receivedAudio = <RealtimeAudioFrame>[];
      final subscription = client.audioFrames.listen(receivedAudio.add);
      addTearDown(subscription.cancel);
      addTearDown(() async {
        client.dispose();
        await client.shutdownComplete;
      });

      expect(client.beginTurn(), isTrue);
      expect(client.sendPcm16Chunk(Uint8List.fromList(<int>[0, 0])), isTrue);
      expect(client.endTurn(), isTrue);

      socket
        ..sendServerJson(_turnEvent('stt_partial', text: 'Hello Aan'))
        ..sendServerJson(_turnEvent('stt_final', text: 'Hello Aanya'))
        ..sendServerJson(_turnEvent('thinking'))
        ..sendServerJson(_turnEvent('text_delta', delta: 'Hello.'))
        ..sendServerJson(_audioHeader())
        ..sendServerBinary(<int>[10, 0, 20, 0])
        ..sendServerJson(_turnEvent('speaking'));
      await _flushEvents();

      expect(client.state.phase, AanyaRealtimePhase.speaking);
      expect(client.state.finalTranscript, 'Hello Aanya');
      expect(client.state.responseText, 'Hello.');
      expect(receivedAudio, hasLength(1));
      expect(receivedAudio.single.header.sequence, 0);
      expect(receivedAudio.single.bytes, <int>[10, 0, 20, 0]);

      socket.sendServerJson(<String, Object?>{
        ..._turnEvent('turn_complete'),
        'metrics': <String, Object?>{'ttfa_ms': 620},
      });
      await _flushEvents();

      expect(client.state.phase, AanyaRealtimePhase.ready);
      expect(client.state.canStartTurn, isTrue);
    },
  );

  test('exposes and clears a recoverable server error', () async {
    final socket = _FakeSocket();
    final client = await _readyClient(socket);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    client
      ..beginTurn()
      ..sendPcm16Chunk(Uint8List.fromList(<int>[0, 0]))
      ..endTurn();

    socket.sendServerJson(<String, Object?>{
      'type': 'recoverable_error',
      'protocol_version': 1,
      'turn_id': 'turn-1',
      'code': 'no_speech',
      'message': 'No speech was detected. Please try again.',
    });
    await _flushEvents();

    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'no_speech');
    expect(client.state.errorIsRecoverable, isTrue);
    expect(client.state.statusLabel, contains('No speech'));

    client.clearRecoverableError();
    expect(client.state.phase, AanyaRealtimePhase.ready);
    expect(client.state.errorCode, isNull);
  });

  test('treats turn_cancelled as an acknowledgement, not an error', () async {
    final socket = _FakeSocket();
    final client = await _readyClient(socket);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    client
      ..beginTurn()
      ..sendPcm16Chunk(Uint8List.fromList(<int>[0, 0]));

    expect(await client.cancelTurn(), isTrue);
    expect(client.state.phase, AanyaRealtimePhase.cancelling);
    expect(client.state.canStartTurn, isFalse);
    socket.sendServerJson(<String, Object?>{
      'type': 'recoverable_error',
      'protocol_version': 1,
      'code': 'turn_cancelled',
      'message': 'The active realtime turn was cancelled.',
    });
    await _flushEvents();

    expect(_sentTypes(socket), contains('cancel_turn'));
    expect(client.state.phase, AanyaRealtimePhase.ready);
    expect(client.state.errorCode, isNull);
    expect(client.state.canStartTurn, isTrue);
  });

  test('preserves a typed fatal error received before session_ready', () async {
    final socket = _FakeSocket();
    final connector = _FakeConnector()..sockets.add(socket);
    final client = _client(connector);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    await client.connect();

    socket.sendServerJson(<String, Object?>{
      'type': 'fatal_error',
      'protocol_version': 1,
      'code': 'unsupported_protocol_version',
      'message': 'Only realtime protocol version 1 is supported.',
    });
    await _flushEvents();

    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'unsupported_protocol_version');
    expect(client.state.errorIsRecoverable, isFalse);
    expect(socket.closeCode, 1002);
    expect(connector.connectCount, 1);
  });

  test('closes without reconnecting after a protocol violation', () async {
    final socket = _FakeSocket();
    final connector = _FakeConnector()..sockets.add(socket);
    final client = _client(connector);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    await client.connect();

    socket.sendServerJson(<String, Object?>{
      ..._sessionReady(),
      'protocol_version': 99,
    });
    await _flushEvents();

    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'unsupported_protocol_version');
    expect(socket.closeCode, 1002);
    expect(connector.connectCount, 1);
  });

  test('rejects a text_sentence sequence that does not start at one', () async {
    final socket = _FakeSocket();
    final client = await _readyClient(socket);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    client
      ..beginTurn()
      ..sendPcm16Chunk(Uint8List.fromList(<int>[0, 0]))
      ..endTurn();

    socket.sendServerJson(<String, Object?>{
      ..._turnEvent('text_sentence', text: 'Hello there.'),
      'sequence': 2,
    });
    await _flushEvents();

    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'invalid_sentence_sequence');
    expect(socket.closeCode, 1002);
  });

  test('reconnects once after an unexpected local connection loss', () async {
    final first = _FakeSocket();
    final second = _FakeSocket();
    final connector = _FakeConnector()
      ..sockets.addAll(<_FakeSocket>[first, second]);
    final client = _client(
      connector,
      reconnectDelays: const <Duration>[Duration.zero],
      reconnectDelay: (_) async {},
    );
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    await client.connect();
    first.sendServerJson(_sessionReady());
    await _flushEvents();

    await first.disconnectFromServer();
    await _flushEvents(8);

    expect(connector.connectCount, 2);
    expect(_sentTypes(second), <String>['session_start']);
    expect(client.state.phase, AanyaRealtimePhase.aiStarting);
    second.sendServerJson(<String, Object?>{
      ..._sessionReady(),
      'session_id': 'session-2',
    });
    await _flushEvents();
    expect(client.state.phase, AanyaRealtimePhase.ready);
    expect(client.state.sessionId, 'session-2');
  });

  test('background cancels the turn, closes, and resumes once', () async {
    final first = _FakeSocket();
    final second = _FakeSocket();
    final connector = _FakeConnector()
      ..sockets.addAll(<_FakeSocket>[first, second]);
    final client = _client(connector);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    await client.connect();
    first.sendServerJson(_sessionReady());
    await _flushEvents();
    client
      ..beginTurn()
      ..sendPcm16Chunk(Uint8List.fromList(<int>[0, 0]));

    await client.setForeground(false);

    expect(_sentTypes(first), <String>[
      'session_start',
      'cancel_turn',
      'session_end',
    ]);
    expect(first.closeCount, 1);
    expect(client.state.phase, AanyaRealtimePhase.offline);
    expect(connector.connectCount, 1);

    await client.setForeground(true);
    expect(connector.connectCount, 2);
    expect(_sentTypes(second), <String>['session_start']);
  });

  test('dispose ends the session and suppresses future reconnects', () async {
    final socket = _FakeSocket();
    final connector = _FakeConnector()..sockets.add(socket);
    final client = _client(connector);
    await client.connect();
    socket.sendServerJson(_sessionReady());
    await _flushEvents();

    client.dispose();
    await client.shutdownComplete;

    expect(client.state.phase, AanyaRealtimePhase.disposed);
    expect(_sentTypes(socket), <String>['session_start', 'session_end']);
    expect(socket.closeCount, 1);
    expect(connector.connectCount, 1);
  });

  test('rejects malformed and over-limit PCM without sending it', () async {
    final socket = _FakeSocket();
    final client = await _readyClient(socket, maxTurnAudioBytes: 4);
    addTearDown(() async {
      client.dispose();
      await client.shutdownComplete;
    });
    expect(client.beginTurn(), isTrue);

    expect(client.sendPcm16Chunk(Uint8List(0)), isFalse);
    expect(client.sendPcm16Chunk(Uint8List.fromList(<int>[1])), isFalse);
    expect(
      client.sendPcm16Chunk(Uint8List.fromList(<int>[1, 2, 3, 4])),
      isTrue,
    );
    expect(client.sendPcm16Chunk(Uint8List.fromList(<int>[5, 6])), isFalse);

    await _flushEvents();
    expect(socket.sentBinary, hasLength(1));
    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'audio_limit_exceeded');
    expect(_sentTypes(socket), contains('cancel_turn'));

    socket.sendServerJson(<String, Object?>{
      'type': 'recoverable_error',
      'protocol_version': 1,
      'code': 'turn_cancelled',
      'message': 'The active realtime turn was cancelled.',
    });
    await _flushEvents();
    expect(client.state.phase, AanyaRealtimePhase.error);
    expect(client.state.errorCode, 'audio_limit_exceeded');
  });
}

AanyaRealtimeClient _client(
  _FakeConnector connector, {
  List<Duration> reconnectDelays = const <Duration>[],
  ReconnectDelay reconnectDelay = _immediateDelay,
  int maxTurnAudioBytes = AiraRealtimeProtocol.maxTurnAudioBytes,
}) {
  return AanyaRealtimeClient(
    companionId: 'aanya',
    config: _config,
    connector: connector,
    handshakeTimeout: const Duration(seconds: 30),
    reconnectDelays: reconnectDelays,
    reconnectDelay: reconnectDelay,
    maxTurnAudioBytes: maxTurnAudioBytes,
  );
}

Future<AanyaRealtimeClient> _readyClient(
  _FakeSocket socket, {
  int maxTurnAudioBytes = AiraRealtimeProtocol.maxTurnAudioBytes,
}) async {
  final connector = _FakeConnector()..sockets.add(socket);
  final client = _client(connector, maxTurnAudioBytes: maxTurnAudioBytes);
  await client.connect();
  socket.sendServerJson(_sessionReady());
  await _flushEvents();
  expect(client.state.phase, AanyaRealtimePhase.ready);
  return client;
}

Future<void> _immediateDelay(Duration _) async {}

Future<void> _flushEvents([int iterations = 4]) async {
  for (var index = 0; index < iterations; index += 1) {
    await Future<void>.delayed(Duration.zero);
  }
}

List<String> _sentTypes(_FakeSocket socket) => socket.sentText
    .map(
      (text) => (jsonDecode(text) as Map<String, dynamic>)['type']! as String,
    )
    .toList(growable: false);

Map<String, Object?> _sessionReady() => <String, Object?>{
  'type': 'session_ready',
  'protocol_version': 1,
  'session_id': 'session-1',
  'companion': 'aanya',
  'ai_disclosure': 'Aanya is a fictional adult AI companion.',
  'readiness': _readiness(),
  'can_process_turns': true,
  'audio_format': <String, Object?>{
    'encoding': 'pcm_s16le',
    'sample_rate_hz': 16000,
    'channels': 1,
  },
};

Map<String, Object?> _readiness({
  String status = 'ready',
  String component = 'ready',
}) => <String, Object?>{
  'status': status,
  'components': <String, Object?>{
    'stt': component,
    'llm': component,
    'tts': component,
  },
  'message': status == 'ready'
      ? 'The warmed local conversation runtime is ready.'
      : 'Local model runtimes are warming.',
};

Map<String, Object?> _turnEvent(String type, {String? text, String? delta}) =>
    <String, Object?>{
      'type': type,
      'protocol_version': 1,
      'turn_id': 'turn-1',
      'text': ?text,
      'delta': ?delta,
    };

Map<String, Object?> _audioHeader() => <String, Object?>{
  ..._turnEvent('audio_chunk'),
  'sequence': 0,
  'encoding': 'pcm_s16le',
  'sample_rate_hz': 16000,
  'channels': 1,
  'byte_length': 4,
};

final AiraApiConfig _config = AiraApiConfig.fromBaseUrl(
  'http://127.0.0.1:8765',
);

final class _FakeConnector implements RealtimeWebSocketConnector {
  final List<_FakeSocket> sockets = <_FakeSocket>[];
  final List<Uri> uris = <Uri>[];
  var connectCount = 0;

  @override
  Future<RealtimeWebSocket> connect(Uri uri) async {
    connectCount += 1;
    uris.add(uri);
    if (sockets.isEmpty) throw StateError('No fake socket queued.');
    return sockets.removeAt(0);
  }
}

final class _FakeSocket implements RealtimeWebSocket {
  final StreamController<Object?> _serverMessages = StreamController<Object?>();
  final List<String> sentText = <String>[];
  final List<List<int>> sentBinary = <List<int>>[];
  var closeCount = 0;
  int? closeCode;
  String? closeReason;

  @override
  Stream<Object?> get messages => _serverMessages.stream;

  @override
  void sendText(String text) => sentText.add(text);

  @override
  void sendBinary(Uint8List bytes) => sentBinary.add(List<int>.from(bytes));

  void sendServerJson(Map<String, Object?> json) {
    _serverMessages.add(jsonEncode(json));
  }

  void sendServerBinary(List<int> bytes) {
    _serverMessages.add(Uint8List.fromList(bytes));
  }

  Future<void> disconnectFromServer() => _serverMessages.close();

  @override
  Future<void> close([int? code, String? reason]) async {
    closeCount += 1;
    closeCode = code;
    closeReason = reason;
    if (!_serverMessages.isClosed) await _serverMessages.close();
  }
}
