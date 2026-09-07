import 'package:female_voice_ai/core/config/aira_api_config.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AiraApiConfig', () {
    test('uses the compile-time environment value or documented fallback', () {
      const configuredBaseUrl = String.fromEnvironment(
        AiraApiConfig.environmentKey,
        defaultValue: AiraApiConfig.fallbackBaseUrl,
      );

      expect(
        AiraApiConfig().normalizedBaseUrl,
        AiraApiConfig.fromBaseUrl(configuredBaseUrl).normalizedBaseUrl,
      );
      expect(
        AiraApiConfig.fromBaseUrl(AiraApiConfig.fallbackBaseUrl)
            .normalizedBaseUrl,
        'http://192.168.1.100:8765',
      );
    });

    test('normalizes trailing slashes and endpoint paths', () {
      final config = AiraApiConfig.fromBaseUrl(
        '  HTTPS://AIRA.LOCAL:8765///  ',
      );

      expect(config.normalizedBaseUrl, 'https://aira.local:8765');
      expect(config.healthUri, Uri.parse('https://aira.local:8765/health'));
      expect(
        config.conversationTurnUri,
        Uri.parse('https://aira.local:8765/v1/conversation/turn'),
      );
      expect(
        config.endpoint(' /v1/conversation/turn '),
        Uri.parse('https://aira.local:8765/v1/conversation/turn'),
      );
    });

    test('rejects invalid origins', () {
      const invalidOrigins = <String>[
        '',
        'not a URL',
        'ftp://aira.local:8765',
        'http:///missing-host',
        'http://user:password@aira.local:8765',
        'http://aira.local:8765/api',
        'http://aira.local:8765?debug=true',
        'http://aira.local:8765/#fragment',
      ];

      for (final origin in invalidOrigins) {
        expect(
          () => AiraApiConfig.fromBaseUrl(origin),
          throwsA(isA<FormatException>()),
          reason: 'Expected "$origin" to be rejected.',
        );
      }
    });

    test('rejects unsafe endpoint paths', () {
      final config = AiraApiConfig.fromBaseUrl('http://127.0.0.1:8765');
      const unsafePaths = <String>[
        '',
        '   ',
        'https://other.example/health',
        '//other.example/health',
        '../health',
        '/v1/../health',
        '/health?verbose=true',
        '/health#details',
      ];

      for (final path in unsafePaths) {
        expect(
          () => config.endpoint(path),
          throwsA(isA<ArgumentError>()),
          reason: 'Expected "$path" to be rejected.',
        );
      }
    });

    test('resolves relative and same-origin turn audio URLs', () {
      final config = AiraApiConfig.fromBaseUrl('http://localhost:8765/');
      const turnId = 'aanya_turn_0123456789abcdef';
      final expected = Uri.parse(
        'http://localhost:8765/v1/conversation/turns/$turnId/audio',
      );

      expect(
        config.resolveTurnAudioUrl('/v1/conversation/turns/$turnId/audio'),
        expected,
      );
      expect(
        config.resolveTurnAudioUrl('v1/conversation/turns/$turnId/audio'),
        expected,
      );
      expect(
        config.resolveTurnAudioUrl(
          'http://LOCALHOST:8765/v1/conversation/turns/$turnId/audio',
        ),
        expected,
      );
    });

    test('rejects cross-origin and unsafe turn audio URLs', () {
      final config = AiraApiConfig.fromBaseUrl('http://localhost:8765');
      const turnId = 'aanya_turn_0123456789abcdef';
      const invalidAudioUrls = <String>[
        '',
        'http://other.example:8765/v1/conversation/turns/$turnId/audio',
        'http://localhost:9999/v1/conversation/turns/$turnId/audio',
        'https://localhost:8765/v1/conversation/turns/$turnId/audio',
        '//other.example/v1/conversation/turns/$turnId/audio',
        'http://user@localhost:8765/v1/conversation/turns/$turnId/audio',
        '/v1/conversation/turns/$turnId/audio?token=secret',
        '/v1/conversation/turns/$turnId/audio#fragment',
        '/v1/conversation/turns/UPPERCASE/audio',
        '/v1/conversation/turns/$turnId/../audio',
        '/some/other/audio.wav',
      ];

      for (final audioUrl in invalidAudioUrls) {
        expect(
          () => config.resolveTurnAudioUrl(audioUrl),
          throwsA(isA<FormatException>()),
          reason: 'Expected "$audioUrl" to be rejected.',
        );
      }
    });
  });
}
