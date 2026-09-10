import 'package:female_voice_ai/features/call/data/android_pcm_voice_playback.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('aira/realtime_pcm_playback');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  tearDown(() {
    messenger.setMockMethodCallHandler(channel, null);
  });

  test(
    'disposing a screen adapter leaves the engine playback bridge reusable',
    () async {
      final methods = <String>[];
      messenger.setMockMethodCallHandler(channel, (call) async {
        methods.add(call.method);
        return null;
      });

      final first = AndroidPcmVoicePlayback();
      await first.start(sampleRateHz: 24000);
      await first.append(Uint8List.fromList(<int>[1, 0]));
      await first.dispose();

      final reopened = AndroidPcmVoicePlayback();
      await reopened.start(sampleRateHz: 24000);
      await reopened.append(Uint8List.fromList(<int>[2, 0]));
      await reopened.finish();
      await reopened.dispose();

      expect(methods, <String>[
        'start',
        'append',
        'stop',
        'start',
        'append',
        'finish',
        'stop',
      ]);
      expect(methods, isNot(contains('dispose')));
    },
  );
}
