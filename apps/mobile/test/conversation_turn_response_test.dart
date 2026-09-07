import 'package:female_voice_ai/features/call/domain/conversation_turn_response.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('ConversationTurnResponse', () {
    test('parses and validates a complete Aanya turn response', () {
      String? resolvedAudioUrl;
      final response = ConversationTurnResponse.fromJson(
        _validResponseJson(),
        resolveAudioUri: (audioUrl) {
          resolvedAudioUrl = audioUrl;
          return Uri.parse('http://localhost:8765$audioUrl');
        },
      );

      expect(response.aiDisclosure, 'Aanya is an AI companion.');
      expect(response.turnId, _turnId);
      expect(response.companion, 'aanya');
      expect(response.rawTranscript, 'Hello Anna');
      expect(response.normalizedTranscript, 'Hello Aanya');
      expect(response.response, 'Hi. It is good to hear from you.');
      expect(response.audioUrl, _audioUrl);
      expect(resolvedAudioUrl, _audioUrl);
      expect(response.audioUri, Uri.parse('http://localhost:8765$_audioUrl'));
    });

    test('rejects every missing required field', () {
      const requiredFields = <String>[
        'ai_disclosure',
        'turn_id',
        'companion',
        'raw_transcript',
        'normalized_transcript',
        'response',
        'audio_url',
      ];

      for (final field in requiredFields) {
        final malformed = _validResponseJson()..remove(field);

        expect(
          () => ConversationTurnResponse.fromJson(
            malformed,
            resolveAudioUri: Uri.parse,
          ),
          throwsA(isA<FormatException>()),
          reason: 'Expected missing "$field" to be rejected.',
        );
      }
    });

    test('rejects blank, non-string, and unsafe response fields', () {
      final malformedResponses = <Map<String, Object?>>[
        _validResponseJson()..['response'] = '   ',
        _validResponseJson()..['raw_transcript'] = 42,
        _validResponseJson()..['companion'] = 'tara',
        _validResponseJson()..['turn_id'] = 'AANYA-TURN',
        _validResponseJson()..['turn_id'] = '../aanya-turn',
        _validResponseJson()..['ai_disclosure'] = 'A warm virtual companion.',
      ];

      for (final malformed in malformedResponses) {
        expect(
          () => ConversationTurnResponse.fromJson(
            malformed,
            resolveAudioUri: Uri.parse,
          ),
          throwsA(isA<FormatException>()),
        );
      }
    });

    test('propagates rejection of an unsafe audio URL', () {
      expect(
        () => ConversationTurnResponse.fromJson(
          _validResponseJson(),
          resolveAudioUri: (_) {
            throw const FormatException('Unsafe origin.');
          },
        ),
        throwsA(isA<FormatException>()),
      );
    });
  });
}

const String _turnId = 'aanya_turn_0123456789abcdef';
const String _audioUrl = '/v1/conversation/turns/$_turnId/audio';

Map<String, Object?> _validResponseJson() => <String, Object?>{
  'ai_disclosure': ' Aanya is an AI companion. ',
  'turn_id': _turnId,
  'companion': 'aanya',
  'raw_transcript': ' Hello Anna ',
  'normalized_transcript': ' Hello Aanya ',
  'response': ' Hi. It is good to hear from you. ',
  'audio_url': ' $_audioUrl ',
};
