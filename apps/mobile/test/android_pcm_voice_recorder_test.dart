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
      recorder.bindCallGeneration(1);
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
      reopened.bindCallGeneration(2);
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
      recorder.bindCallGeneration(3);
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

  test('native recorder requires a bound call generation', () async {
    final recorder = AndroidPcmVoiceRecorder(
      permissionChecker: () async => true,
    );

    await expectLater(recorder.startPcm16Stream(), throwsStateError);
    await recorder.dispose();
  });

  test(
    'cancelling a never-listened stream does not wedge the next start',
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
      )..bindCallGeneration(7);

      // The call controller can abandon a started stream (stale operation or
      // start timeout) without ever listening to it.
      await recorder.startPcm16Stream();
      await recorder.cancel().timeout(const Duration(seconds: 1));

      final next = await recorder.startPcm16Stream().timeout(
        const Duration(seconds: 1),
      );
      final nextDone = next.drain<void>();
      await recorder.stopStream().timeout(const Duration(seconds: 1));
      await nextDone.timeout(const Duration(seconds: 1));
      await recorder.dispose();
    },
  );

  test('failed native start throws instead of hanging', () async {
    messenger.setMockMethodCallHandler(capture, (call) async {
      if (call.method == 'start') {
        throw PlatformException(
          code: 'capture_busy',
          message: 'Microphone capture is stopping.',
        );
      }
      return <String, Object?>{'state': 'stopped'};
    });
    final recorder = AndroidPcmVoiceRecorder(
      permissionChecker: () async => true,
    )..bindCallGeneration(8);

    await expectLater(
      recorder.startPcm16Stream().timeout(const Duration(seconds: 1)),
      throwsA(
        isA<RealtimeMicrophoneException>().having(
          (error) => error.code,
          'code',
          'capture_busy',
        ),
      ),
    );
    await recorder.dispose();
  });

  test('stale old-generation cancel cannot stop newer native capture', () async {
    var activeGeneration = 0;
    var recording = false;
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(capture, (call) async {
      calls.add(call);
      final arguments = (call.arguments as Map<Object?, Object?>?) ?? const {};
      final generation = arguments['callGeneration'] as int?;
      if (call.method == 'start') {
        activeGeneration = generation!;
        recording = true;
      } else if (call.method == 'cancel' && generation == activeGeneration) {
        recording = false;
      }
      return <String, Object?>{
        'state': recording ? 'recording' : 'stopped',
        'capturedBytes': 0,
        'capturedFrames': 0,
      };
    });
    final oldRecorder = AndroidPcmVoiceRecorder(
      permissionChecker: () async => true,
    )..bindCallGeneration(41);
    final newRecorder = AndroidPcmVoiceRecorder(
      permissionChecker: () async => true,
    )..bindCallGeneration(42);

    await oldRecorder.startPcm16Stream();
    await newRecorder.startPcm16Stream();
    await oldRecorder.cancel();

    expect(activeGeneration, 42);
    expect(recording, isTrue);
    expect(
      calls.where((call) => call.method == 'cancel').last.arguments,
      <String, Object?>{'callGeneration': 41},
    );

    await newRecorder.cancel();
    expect(recording, isFalse);
    await oldRecorder.dispose();
    await newRecorder.dispose();
  });
}
