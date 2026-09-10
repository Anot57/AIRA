import 'dart:async';

import 'package:female_voice_ai/features/call/data/android_pcm_voice_recorder.dart';
import 'package:female_voice_ai/features/call/application/voice_io.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const capture = MethodChannel('aira/realtime_pcm_capture');
  const events = MethodChannel('aira/realtime_pcm_capture/events');
  const codec = StandardMethodCodec();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  setUp(() {
    messenger.setMockMethodCallHandler(events, (call) async => null);
  });

  tearDown(() {
    messenger.setMockMethodCallHandler(capture, null);
    messenger.setMockMethodCallHandler(events, null);
  });

  Future<void> emit(Map<String, Object?> event) async {
    await messenger.handlePlatformMessage(
      'aira/realtime_pcm_capture/events',
      codec.encodeSuccessEnvelope(event),
      (_) {},
    );
  }

  test(
    'native adapter forwards exact PCM and waits for terminal stop',
    () async {
      final methods = <String>[];
      messenger.setMockMethodCallHandler(capture, (call) async {
        methods.add(call.method);
        return <String, Object?>{
          'state': call.method == 'start' ? 'recording' : 'stopped',
          'capturedBytes': call.method == 'start' ? 0 : 4,
          'capturedFrames': call.method == 'start' ? 0 : 2,
        };
      });
      final recorder = AndroidPcmVoiceRecorder(
        permissionChecker: () async => true,
      );
      final stream = await recorder.startPcm16Stream();
      final received = <List<int>>[];
      final done = Completer<void>();
      stream.listen((bytes) => received.add(bytes), onDone: done.complete);

      await emit(<String, Object?>{
        'type': 'pcm',
        'state': 'recording',
        'bytes': Uint8List.fromList(<int>[1, 0, 2, 0]),
        'capturedBytes': 4,
        'capturedFrames': 2,
      });
      await recorder.stopStream();
      await done.future;

      expect(received, <List<int>>[
        <int>[1, 0, 2, 0],
      ]);
      expect(recorder.deliveredBytes, 4);
      expect(recorder.capturedBytes, 4);

      final second = await recorder.startPcm16Stream();
      final secondDone = second.drain<void>();
      await recorder.cancel();
      await secondDone;

      await recorder.dispose();
      final reopened = AndroidPcmVoiceRecorder(
        permissionChecker: () async => true,
      );
      final reopenedStream = await reopened.startPcm16Stream();
      final reopenedDone = reopenedStream.drain<void>();
      await reopened.cancel();
      await reopenedDone;
      await reopened.dispose();

      expect(methods, <String>[
        'start',
        'stop',
        'start',
        'cancel',
        'start',
        'cancel',
      ]);
      expect(methods, isNot(contains('dispose')));
    },
  );

  test(
    'native zero-audio failure is surfaced as a typed recorder error',
    () async {
      messenger.setMockMethodCallHandler(capture, (call) async {
        return <String, Object?>{
          'state': call.method == 'start' ? 'recording' : 'stopped',
          'capturedBytes': 0,
          'capturedFrames': 0,
        };
      });
      final recorder = AndroidPcmVoiceRecorder(
        permissionChecker: () async => true,
      );
      final first = await recorder.startPcm16Stream();
      final receivedError = Completer<Object>();
      first.listen(
        (_) {},
        onError: (Object error) => receivedError.complete(error),
      );
      await emit(<String, Object?>{
        'type': 'error',
        'state': 'error',
        'code': 'zero_audio_timeout',
        'message': 'No PCM arrived.',
        'capturedBytes': 0,
        'capturedFrames': 0,
      });
      expect(
        await receivedError.future,
        isA<RealtimeMicrophoneException>().having(
          (value) => value.code,
          'code',
          'zero_audio_timeout',
        ),
      );
    },
  );
}
