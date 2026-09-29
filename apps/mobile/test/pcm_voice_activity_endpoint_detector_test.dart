import 'dart:typed_data';

import 'package:female_voice_ai/features/call/application/pcm_voice_activity_endpoint_detector.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('PcmVoiceActivityEndpointDetector noise rejection', () {
    test('complete silence creates no conversation turn', () {
      _expectNoSpeech(List<int>.filled(750, 0));
    });

    test('low room noise creates no conversation turn', () {
      _expectNoSpeech(List<int>.generate(750, (index) => 220 + index % 25));
    });

    test('steady fan or AC-like noise is learned without onset', () {
      _expectNoSpeech(List<int>.generate(750, (index) => 900 + index % 8));
    });

    test('isolated click creates no conversation turn', () {
      _expectNoSpeech(<int>[...List.filled(20, 100), 9000, ...List.filled(30, 100)]);
    });

    test('one brief loud impulse creates no conversation turn', () {
      _expectNoSpeech(<int>[12000, 0, 0, 0, 0, 0]);
    });

    test('several separated noise spikes create no conversation turn', () {
      _expectNoSpeech(<int>[100, 7000, 120, 100, 6000, 90, 100, 8000, 110]);
    });

    test('quiet real speech remains detectable', () {
      final observations = _observe(<int>[700, 790, 680]);
      expect(observations.last.speechStarted, isTrue);
    });

    test('normal real speech remains detectable', () {
      final observations = _observe(<int>[1800, 2400, 1700]);
      expect(observations.last.speechStarted, isTrue);
    });

    for (final word in <String>['Hi', 'Yes', 'No']) {
      test('short "$word" remains detectable', () {
        final observations = _observe(<int>[900, 1200, 850]);
        expect(observations.last.speechStarted, isTrue);
        expect(observations.last.speechOnset, Duration.zero);
      });
    }

    test('speech begins successfully after background noise', () {
      final detector = PcmVoiceActivityEndpointDetector();
      var frame = 0;
      for (; frame < 50; frame += 1) {
        final observation = detector.observe(
          _pcm(amplitude: 300 + frame % 10),
          Duration(milliseconds: frame * 40),
        );
        expect(observation.speechStarted, isFalse);
      }
      PcmVoiceActivityObservation? onset;
      for (final amplitude in <int>[900, 1200, 850]) {
        onset = detector.observe(
          _pcm(amplitude: amplitude),
          Duration(milliseconds: frame++ * 40),
        );
      }
      expect(onset!.speechStarted, isTrue);
    });
  });

  group('PcmVoiceActivityEndpointDetector endpoint', () {
    for (final pauseMs in <int>[1000, 2900]) {
      test('speech with $pauseMs ms pause remains one turn', () {
        final detector = _startedDetector();
        final lastSpeech = detector.lastSpeech!;
        detector.observe(
          _pcm(amplitude: 0),
          lastSpeech + Duration(milliseconds: pauseMs),
        );
        final resumed = detector.observe(
          _pcm(amplitude: 1800),
          lastSpeech + Duration(milliseconds: pauseMs + 40),
        );

        expect(resumed.endpointReached, isFalse);
        expect(resumed.state, PcmVoiceActivityState.speechActive);
      });
    }

    test('speech with at least 3000 ms ending silence finalizes once', () {
      final detector = _startedDetector();
      final lastSpeech = detector.lastSpeech!;
      final endpoint = detector.observe(
        _pcm(amplitude: 0),
        lastSpeech + const Duration(milliseconds: 3000),
      );
      final duplicate = detector.observe(
        _pcm(amplitude: 0),
        lastSpeech + const Duration(milliseconds: 4000),
      );

      expect(endpoint.endpointReached, isTrue);
      expect(endpoint.state, PcmVoiceActivityState.finalized);
      expect(duplicate.endpointReached, isFalse);
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

void _expectNoSpeech(List<int> amplitudes) {
  final observations = _observe(amplitudes);
  expect(observations.any((value) => value.speechStarted), isFalse);
  expect(observations.any((value) => value.endpointReached), isFalse);
}

List<PcmVoiceActivityObservation> _observe(List<int> amplitudes) {
  final detector = PcmVoiceActivityEndpointDetector();
  return <PcmVoiceActivityObservation>[
    for (var frame = 0; frame < amplitudes.length; frame += 1)
      detector.observe(
        _pcm(amplitude: amplitudes[frame]),
        Duration(milliseconds: frame * 40),
      ),
  ];
}

PcmVoiceActivityEndpointDetector _startedDetector() {
  final detector = PcmVoiceActivityEndpointDetector();
  const amplitudes = <int>[1800, 2400, 1700];
  for (var frame = 0; frame < amplitudes.length; frame += 1) {
    detector.observe(
      _pcm(amplitude: amplitudes[frame]),
      Duration(milliseconds: frame * 40),
    );
  }
  expect(detector.hasSpeech, isTrue);
  return detector;
}

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
