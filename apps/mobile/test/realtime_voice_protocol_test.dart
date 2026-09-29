import 'dart:convert';

import 'package:female_voice_ai/features/call/domain/realtime_voice_protocol.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('realtime client messages', () {
    test('session_start advertises the required Aanya PCM format', () {
      final json = jsonDecode(
        const RealtimeSessionStartMessage().toWireText(),
      ) as Map<String, dynamic>;

      expect(json['type'], 'session_start');
      expect(json['protocol_version'], 1);
      expect(json['companion'], 'aanya');
      expect(json['audio_format'], <String, Object?>{
        'encoding': 'pcm_s16le',
        'sample_rate_hz': 16000,
        'channels': 1,
      });
    });

    test('turn and session controls are versioned', () {
      for (final message in const <RealtimeClientMessage>[
        RealtimeEndOfTurnMessage(),
        RealtimeCancelTurnMessage(),
        RealtimeSessionEndMessage(),
      ]) {
        final json = jsonDecode(message.toWireText()) as Map<String, dynamic>;
        expect(json.keys, containsAll(<String>['type', 'protocol_version']));
        expect(json['protocol_version'], AiraRealtimeProtocol.version);
      }
    });
  });

  group('RealtimeServerEventParser', () {
    Map<String, Object?> safetyEscalation() => <String, Object?>{
      'type': 'safety_escalation',
      'protocol_version': 1,
      'turn_id': 'turn-1',
      'kind': 'crisis_resources',
      'resources': <Map<String, Object?>>[
        <String, Object?>{'label': 'Emergency services', 'phone': '112'},
        <String, Object?>{
          'label': 'Tele-MANAS mental health helpline',
          'phone': '14416',
        },
      ],
    };

    test('parses crisis resources from a safety escalation', () {
      final event = RealtimeServerEventParser.parse(
        jsonEncode(safetyEscalation()),
      );

      expect(event, isA<RealtimeSafetyEscalationEvent>());
      final escalation = event as RealtimeSafetyEscalationEvent;
      expect(escalation.turnId, 'turn-1');
      expect(
        escalation.resources.map((resource) => resource.phone),
        <String>['112', '14416'],
      );
      expect(escalation.resources.first.label, 'Emergency services');
    });

    test('rejects malformed safety escalations', () {
      final invalid = <Map<String, Object?>>[
        <String, Object?>{...safetyEscalation(), 'kind': 'flirt'},
        <String, Object?>{...safetyEscalation(), 'resources': <Object?>[]},
        <String, Object?>{
          ...safetyEscalation(),
          'resources': <Map<String, Object?>>[
            <String, Object?>{'label': 'Emergency', 'phone': 'call me'},
          ],
        },
      ];
      for (final payload in invalid) {
        expect(
          () => RealtimeServerEventParser.parse(jsonEncode(payload)),
          _protocolError('invalid_safety_escalation'),
        );
      }
    });

    test('parses and validates session_ready', () {
      final event = RealtimeServerEventParser.parse(
        jsonEncode(_sessionReady()),
      );

      expect(event, isA<RealtimeSessionReadyEvent>());
      final ready = event as RealtimeSessionReadyEvent;
      expect(ready.sessionId, 'session-1');
      expect(ready.companion, 'aanya');
      expect(ready.aiDisclosure, contains('AI companion'));
      expect(ready.readiness.status, 'ready');
      expect(ready.readiness.components['tts'], 'ready');
      expect(ready.canProcessTurns, isTrue);
      expect(ready.audioFormat.encoding, 'pcm_s16le');
      expect(ready.audioFormat.sampleRateHz, 16000);
      expect(ready.audioFormat.channels, 1);
    });

    test('parses an observable not-ready transport without claiming ready', () {
      final event = RealtimeServerEventParser.parse(
        jsonEncode(<String, Object?>{
          ..._sessionReady(),
          'readiness': _readiness(status: 'warming', component: 'loading'),
          'can_process_turns': false,
        }),
      ) as RealtimeSessionReadyEvent;

      expect(event.readiness.status, 'warming');
      expect(event.readiness.components.values, everyElement('loading'));
      expect(event.canProcessTurns, isFalse);
    });

    test('rejects a ready snapshot with a non-ready component', () {
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._sessionReady(),
            'readiness': _readiness(status: 'ready', component: 'loading'),
          }),
        ),
        _protocolError('invalid_readiness'),
      );
    });

    test('parses typed turn, audio, completion, and error events', () {
      final events = <RealtimeServerEvent>[
        RealtimeServerEventParser.parse(
          jsonEncode(_turnEvent('stt_partial', text: 'Hello Aan')),
        ),
        RealtimeServerEventParser.parse(
          jsonEncode(_turnEvent('stt_final', text: 'Hello Aanya')),
        ),
        RealtimeServerEventParser.parse(jsonEncode(_turnEvent('thinking'))),
        RealtimeServerEventParser.parse(
          jsonEncode(_turnEvent('text_delta', delta: 'Hello')),
        ),
        RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._turnEvent('text_sentence', text: 'Hello there.'),
            'sequence': 1,
          }),
        ),
        RealtimeServerEventParser.parse(jsonEncode(_audioHeader())),
        RealtimeServerEventParser.parse(jsonEncode(_turnEvent('speaking'))),
        RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._turnEvent('turn_complete'),
            'metrics': <String, Object?>{'ttfa_ms': 620, 'total_ms': 900.5},
          }),
        ),
        RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            'type': 'recoverable_error',
            'protocol_version': 1,
            'turn_id': 'turn-1',
            'code': 'no_speech',
            'message': 'No speech was detected.',
          }),
        ),
      ];

      expect(events[0], isA<RealtimeSttPartialEvent>());
      expect(events[1], isA<RealtimeSttFinalEvent>());
      expect(events[2], isA<RealtimeThinkingEvent>());
      expect(events[3], isA<RealtimeTextDeltaEvent>());
      expect(events[4], isA<RealtimeTextSentenceEvent>());
      expect(events[5], isA<RealtimeAudioChunkEvent>());
      expect(events[6], isA<RealtimeSpeakingEvent>());
      expect(events[7], isA<RealtimeTurnCompleteEvent>());
      expect(events[8], isA<RealtimeErrorEvent>());
      expect((events[7] as RealtimeTurnCompleteEvent).metrics['ttfa_ms'], 620);
      expect((events[8] as RealtimeErrorEvent).recoverable, isTrue);
      expect((events[4] as RealtimeTextSentenceEvent).sequence, 1);
      final audio = events[5] as RealtimeAudioChunkEvent;
      expect(audio.audioDurationMs, 0.083);
      expect(audio.synthesisMs, 25);
      expect(audio.realtimeFactor, 1.25);
    });

    test('rejects unsupported versions and companions', () {
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._sessionReady(),
            'protocol_version': 2,
          }),
        ),
        _protocolError('unsupported_protocol_version'),
      );
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._sessionReady(),
            'companion': 'tara',
          }),
        ),
        _protocolError('unsupported_companion'),
      );
    });

    test('requires explicit AI disclosure and exact audio format', () {
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._sessionReady(),
            'ai_disclosure': 'Aanya is your companion.',
          }),
        ),
        _protocolError('missing_ai_disclosure'),
      );
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._sessionReady(),
            'audio_format': <String, Object?>{
              'encoding': 'pcm_f32le',
              'sample_rate_hz': 48000,
              'channels': 2,
            },
          }),
        ),
        _protocolError('unsupported_audio_format'),
      );
    });

    test('rejects malformed, oversized, and invalid audio messages', () {
      expect(
        () => RealtimeServerEventParser.parse('{broken'),
        _protocolError('malformed_json'),
      );
      expect(
        () => RealtimeServerEventParser.parse(
          'x' * (AiraRealtimeProtocol.maxServerControlBytes + 1),
        ),
        _protocolError('message_too_large'),
      );
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._audioHeader(),
            'byte_length': AiraRealtimeProtocol.maxFrameBytes + 2,
          }),
        ),
        _protocolError('invalid_audio_chunk'),
      );
      expect(
        () => RealtimeServerEventParser.parse(
          jsonEncode(<String, Object?>{
            ..._audioHeader(),
            'synthesis_ms': -1.0,
          }),
        ),
        _protocolError('invalid_synthesis_ms'),
      );
    });
  });
}

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
  'sample_rate_hz': 24000,
  'channels': 1,
  'byte_length': 4,
  'audio_duration_ms': 0.083,
  'synthesis_ms': 25.0,
  'realtime_factor': 1.25,
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

Matcher _protocolError(String code) => throwsA(
  isA<RealtimeProtocolException>().having((error) => error.code, 'code', code),
);
