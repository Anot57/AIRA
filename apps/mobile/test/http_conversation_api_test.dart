import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:female_voice_ai/core/config/aira_api_config.dart';
import 'package:female_voice_ai/features/call/data/conversation_api.dart';
import 'package:female_voice_ai/features/call/data/http_conversation_api.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

void main() {
  late Directory temporaryDirectory;
  late File recording;

  setUp(() async {
    temporaryDirectory = await Directory.systemTemp.createTemp(
      'aira_http_conversation_api_test_',
    );
    recording = File(
      '${temporaryDirectory.path}${Platform.pathSeparator}private-source.wav',
    );
    await recording.writeAsBytes(_minimalWavBytes, flush: true);
  });

  tearDown(() async {
    if (await temporaryDirectory.exists()) {
      await temporaryDirectory.delete(recursive: true);
    }
  });

  group('HttpConversationApi health', () {
    test('returns true only for the expected health response', () async {
      var attempts = 0;
      final api = _apiWithHandler((request) async {
        attempts += 1;
        expect(request.method, 'GET');
        expect(request.url, Uri.parse('http://127.0.0.1:8765/health'));
        return http.Response(
          jsonEncode(<String, Object?>{
            'status': 'ok',
            'service': 'aira-local-voice-api',
          }),
          200,
        );
      });
      addTearDown(api.dispose);

      expect(await api.checkHealth(), isTrue);
      expect(attempts, 1);
    });

    test(
      'returns false for malformed JSON without leaking an exception',
      () async {
        final api = _apiWithHandler(
          (_) async => http.Response('{not-json', 200),
        );
        addTearDown(api.dispose);

        expect(await api.checkHealth(), isFalse);
      },
    );

    test('returns false when the service is unreachable', () async {
      final api = _apiWithHandler(
        (request) async => throw http.ClientException('offline', request.url),
      );
      addTearDown(api.dispose);

      expect(await api.checkHealth(), isFalse);
    });
  });

  group('HttpConversationApi conversation turn', () {
    test(
      'sends one Aanya WAV multipart POST and parses the response',
      () async {
        var attempts = 0;
        final api = _apiWithHandler((request) async {
          attempts += 1;
          expect(request.method, 'POST');
          expect(
            request.url,
            Uri.parse('http://127.0.0.1:8765/v1/conversation/turn'),
          );
          expect(
            request.headers['content-type'],
            startsWith('multipart/form-data; boundary='),
          );

          final multipartBody = latin1.decode(request.bodyBytes);
          final lowerCaseBody = multipartBody.toLowerCase();
          expect(multipartBody, contains('name="companion"'));
          expect(multipartBody, contains('\r\n\r\naanya\r\n'));
          expect(multipartBody, contains('name="audio"'));
          expect(multipartBody, contains('filename="recording.wav"'));
          expect(lowerCaseBody, contains('content-type: audio/wav'));
          expect(multipartBody, isNot(contains('private-source.wav')));
          expect(request.bodyBytes, containsAllInOrder(_minimalWavBytes));

          return http.Response(jsonEncode(_validResponseJson()), 200);
        });
        addTearDown(api.dispose);

        final result = await api.createAanyaTurn(recording.path);

        expect(attempts, 1);
        expect(result.turnId, _turnId);
        expect(result.companion, 'aanya');
        expect(result.normalizedTranscript, 'Hello Aanya');
        expect(result.response, 'Hello. It is good to hear from you.');
        expect(result.audioUri, Uri.parse('http://127.0.0.1:8765$_audioUrl'));
      },
    );

    test('maps malformed JSON to unexpectedResponse', () async {
      final api = _apiWithHandler((_) async => http.Response('{not-json', 200));
      addTearDown(api.dispose);

      await expectLater(
        api.createAanyaTurn(recording.path),
        throwsA(_apiError(ConversationApiErrorKind.unexpectedResponse)),
      );
    });

    test('maps a missing required field to unexpectedResponse', () async {
      final incomplete = _validResponseJson()..remove('response');
      final api = _apiWithHandler(
        (_) async => http.Response(jsonEncode(incomplete), 200),
      );
      addTearDown(api.dispose);

      await expectLater(
        api.createAanyaTurn(recording.path),
        throwsA(_apiError(ConversationApiErrorKind.unexpectedResponse)),
      );
    });

    for (final entry in <int, ConversationApiErrorKind>{
      400: ConversationApiErrorKind.invalidRequest,
      500: ConversationApiErrorKind.processingFailure,
      503: ConversationApiErrorKind.serviceBusy,
    }.entries) {
      test('maps HTTP ${entry.key} and never retries the POST', () async {
        var attempts = 0;
        final api = _apiWithHandler((_) async {
          attempts += 1;
          return http.Response('{}', entry.key);
        });
        addTearDown(api.dispose);

        await expectLater(
          api.createAanyaTurn(recording.path),
          throwsA(_apiError(entry.value)),
        );
        expect(attempts, 1);
      });
    }

    test('maps a request timeout without retrying', () async {
      var attempts = 0;
      final pendingResponse = Completer<http.Response>();
      addTearDown(() {
        if (!pendingResponse.isCompleted) {
          pendingResponse.complete(http.Response('{}', 503));
        }
      });
      final api = _apiWithHandler((_) {
        attempts += 1;
        return pendingResponse.future;
      }, conversationTimeout: const Duration(milliseconds: 20));
      addTearDown(api.dispose);

      await expectLater(
        api.createAanyaTurn(recording.path),
        throwsA(_apiError(ConversationApiErrorKind.timeout)),
      );
      expect(attempts, 1);
    });

    test('maps a connection failure without retrying', () async {
      var attempts = 0;
      final api = _apiWithHandler((request) async {
        attempts += 1;
        throw http.ClientException('connection refused', request.url);
      });
      addTearDown(api.dispose);

      await expectLater(
        api.createAanyaTurn(recording.path),
        throwsA(_apiError(ConversationApiErrorKind.unavailable)),
      );
      expect(attempts, 1);
    });

    test('rejects an invalid local recording before making a POST', () async {
      var attempts = 0;
      final api = _apiWithHandler((_) async {
        attempts += 1;
        return http.Response(jsonEncode(_validResponseJson()), 200);
      });
      addTearDown(api.dispose);

      final emptyRecording = File(
        '${temporaryDirectory.path}${Platform.pathSeparator}empty.wav',
      );
      await emptyRecording.create();

      await expectLater(
        api.createAanyaTurn(emptyRecording.path),
        throwsA(_apiError(ConversationApiErrorKind.invalidRequest)),
      );
      expect(attempts, 0);
    });
  });
}

HttpConversationApi _apiWithHandler(
  Future<http.Response> Function(http.Request request) handler, {
  Duration conversationTimeout = const Duration(seconds: 1),
}) {
  return HttpConversationApi(
    config: AiraApiConfig.fromBaseUrl('http://127.0.0.1:8765'),
    client: MockClient(handler),
    healthTimeout: const Duration(seconds: 1),
    conversationTimeout: conversationTimeout,
  );
}

Matcher _apiError(ConversationApiErrorKind kind) {
  return isA<ConversationApiException>().having(
    (error) => error.kind,
    'kind',
    kind,
  );
}

const String _turnId = 'aanya_turn_0123456789abcdef';
const String _audioUrl = '/v1/conversation/turns/$_turnId/audio';

Map<String, Object?> _validResponseJson() => <String, Object?>{
  'ai_disclosure': 'Aanya is an AI companion.',
  'turn_id': _turnId,
  'companion': 'aanya',
  'raw_transcript': 'Hello Anna',
  'normalized_transcript': 'Hello Aanya',
  'response': 'Hello. It is good to hear from you.',
  'audio_url': _audioUrl,
};

const List<int> _minimalWavBytes = <int>[
  0x52,
  0x49,
  0x46,
  0x46,
  0x24,
  0x00,
  0x00,
  0x00,
  0x57,
  0x41,
  0x56,
  0x45,
  0x66,
  0x6d,
  0x74,
  0x20,
  0x10,
  0x00,
  0x00,
  0x00,
  0x01,
  0x00,
  0x01,
  0x00,
  0x80,
  0x3e,
  0x00,
  0x00,
  0x00,
  0x7d,
  0x00,
  0x00,
  0x02,
  0x00,
  0x10,
  0x00,
  0x64,
  0x61,
  0x74,
  0x61,
  0x00,
  0x00,
  0x00,
  0x00,
];
