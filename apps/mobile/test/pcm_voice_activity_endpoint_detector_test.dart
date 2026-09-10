import 'dart:typed_data';

import 'package:female_voice_ai/features/call/application/pcm_voice_activity_endpoint_detector.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('PcmVoiceActivityEndpointDetector', () {
    for (final pauseMs in <int>[500, 1500, 2999]) {
      test('speech + $pauseMs ms silence + speech remains one turn', () {
        final detector = _startedDetector();
        final quiet = _pcm(amplitude: 0);
        final voice = _pcm(amplitude: 5000);

        expect(
          detector.observe(quiet, const Duration(milliseconds: 100)),
          isNot(_endpoint),
        );
        final resumed = detector.observe(
          voice,
          Duration(milliseconds: 80 + pauseMs),
        );

        expect(resumed.endpointReached, isFalse);
        expect(resumed.state, PcmVoiceActivityState.speechActive);
      });
    }

    test('speech + 3000 ms sustained silence finalizes exactly once', () {
      final detector = _startedDetector();
      final quiet = _pcm(amplitude: 0);

      final endpoint = detector.observe(
        quiet,
        const Duration(milliseconds: 3080),
      );
      final duplicate = detector.observe(
        quiet,
        const Duration(milliseconds: 4000),
      );

      expect(endpoint.endpointReached, isTrue);
      expect(endpoint.state, PcmVoiceActivityState.finalized);
      expect(duplicate.endpointReached, isFalse);
    });

    test('no speech for 30 seconds creates no endpoint', () {
      final detector = PcmVoiceActivityEndpointDetector();
      final quiet = _pcm(amplitude: 0);

      for (var second = 0; second <= 30; second += 1) {
        final observation = detector.observe(
          quiet,
          Duration(seconds: second),
        );
        expect(observation.speechStarted, isFalse);
        expect(observation.endpointReached, isFalse);
      }
      expect(detector.state, PcmVoiceActivityState.waitingForSpeech);
    });

    test('stable background noise without speech creates no endpoint', () {
      final detector = PcmVoiceActivityEndpointDetector();
      final roomNoise = _pcm(amplitude: 300);

      for (var frame = 0; frame < 750; frame += 1) {
        final observation = detector.observe(
          roomNoise,
          Duration(milliseconds: frame * 40),
        );
        expect(observation.speechStarted, isFalse);
        expect(observation.endpointReached, isFalse);
      }
    });

    test('quiet speech clears the lower onset floor after three frames', () {
      final detector = PcmVoiceActivityEndpointDetector();
      final quietSpeech = _pcm(amplitude: 700);

      expect(detector.observe(quietSpeech, Duration.zero).speechStarted, isFalse);
      expect(
        detector
            .observe(quietSpeech, const Duration(milliseconds: 40))
            .speechStarted,
        isFalse,
      );
      final onset = detector.observe(
        quietSpeech,
        const Duration(milliseconds: 80),
      );

      expect(onset.speechStarted, isTrue);
      expect(onset.state, PcmVoiceActivityState.speechActive);
    });

    test('multiple natural pauses retain one utterance until final silence', () {
      final detector = _startedDetector();
      final quiet = _pcm(amplitude: 0);
      final voice = _pcm(amplitude: 5000);

      detector.observe(quiet, const Duration(milliseconds: 200));
      detector.observe(voice, const Duration(milliseconds: 1080));
      detector.observe(quiet, const Duration(milliseconds: 1500));
      detector.observe(voice, const Duration(milliseconds: 3080));
      final endpoint = detector.observe(
        quiet,
        const Duration(milliseconds: 6080),
      );

      expect(endpoint.endpointReached, isTrue);
      expect(detector.speechOnset, const Duration(milliseconds: 80));
    });

    test('invalid or non-monotonic PCM observations are rejected', () {
      final detector = PcmVoiceActivityEndpointDetector();
      expect(
        () => detector.observe(Uint8List(1), Duration.zero),
        throwsFormatException,
      );
      detector.observe(_pcm(amplitude: 0), const Duration(seconds: 1));
      expect(
        () => detector.observe(_pcm(amplitude: 0), Duration.zero),
        throwsArgumentError,
      );
    });
  });
}

Matcher get _endpoint => isA<PcmVoiceActivityObservation>().having(
  (observation) => observation.endpointReached,
  'endpointReached',
  isTrue,
);

PcmVoiceActivityEndpointDetector _startedDetector() {
  final detector = PcmVoiceActivityEndpointDetector();
  final voice = _pcm(amplitude: 5000);
  detector
    ..observe(voice, Duration.zero)
    ..observe(voice, const Duration(milliseconds: 40));
  final onset = detector.observe(voice, const Duration(milliseconds: 80));
  expect(onset.speechStarted, isTrue);
  return detector;
}

Uint8List _pcm({required int amplitude, int samples = 640}) {
  final bytes = Uint8List(samples * 2);
  final data = ByteData.sublistView(bytes);
  for (var sample = 0; sample < samples; sample += 1) {
    // Alternating polarity avoids a DC-only test signal while retaining a
    // deterministic RMS equal to the requested amplitude.
    data.setInt16(
      sample * 2,
      sample.isEven ? amplitude : -amplitude,
      Endian.little,
    );
  }
  return bytes;
}
