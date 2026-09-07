import 'dart:async';
import 'dart:convert';

import 'package:female_voice_ai/app/aira_app.dart';
import 'package:female_voice_ai/features/call/aanya_voice_call_screen.dart';
import 'package:female_voice_ai/features/call/application/aanya_voice_call_controller.dart';
import 'package:female_voice_ai/features/call/application/voice_io.dart';
import 'package:female_voice_ai/features/call/data/conversation_api.dart';
import 'package:female_voice_ai/features/call/domain/conversation_turn_response.dart';
import 'package:female_voice_ai/features/call/mock_voice_call_screen.dart';
import 'package:female_voice_ai/features/companions/data/companion_catalog.dart';
import 'package:female_voice_ai/features/companions/domain/companion.dart';
import 'package:female_voice_ai/features/companions/domain/companion_filter.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('Aanya voice call screen', () {
    testWidgets('shows AI disclosure and local connection state', (
      tester,
    ) async {
      _usePhoneViewport(tester);
      final health = Completer<bool>();
      final api = _FakeConversationApi(healthCheck: () => health.future);
      final harness = _VoiceHarness(api: api);

      await _pumpVoiceScreen(tester, harness.controller);

      expect(find.byKey(const Key('aanya_voice_call_screen')), findsOneWidget);
      expect(find.byKey(const Key('aanya_ai_disclosure')), findsOneWidget);
      expect(find.text('AI companion'), findsOneWidget);
      expect(
        tester.widget<Text>(find.byKey(const Key('voice_status'))).data,
        contains('Checking'),
      );

      health.complete(true);
      await _pumpAsyncWork(tester);

      expect(find.textContaining('Connected'), findsOneWidget);
      expect(find.text('Hold to talk'), findsWidgets);
      expect(api.healthCheckCount, 1);

      await _disposeVoiceScreen(tester, harness.controller);
    });

    testWidgets(
      'shows listening, processing, speaking, and appends normalized turns',
      (tester) async {
        _usePhoneViewport(tester);
        final api = _FakeConversationApi();
        final firstResponse = Completer<ConversationTurnResponse>();
        final secondResponse = Completer<ConversationTurnResponse>();
        api
          ..enqueueTurn((_) => firstResponse.future)
          ..enqueueTurn((_) => secondResponse.future);
        final harness = _VoiceHarness(api: api);

        await _pumpVoiceScreen(tester, harness.controller);
        await _pumpAsyncWork(tester);
        expect(find.textContaining('Connected'), findsOneWidget);

        final firstGesture = await tester.startGesture(
          tester.getCenter(find.byKey(const Key('aanya_microphone_control'))),
        );
        await _pumpAsyncWork(tester);

        expect(find.textContaining('Listening'), findsOneWidget);
        expect(harness.recorder.startCount, 1);

        await firstGesture.up();
        await _pumpAsyncWork(tester);

        expect(find.textContaining('Understanding'), findsOneWidget);
        expect(api.turnAudioPaths, hasLength(1));

        firstResponse.complete(
          _response(
            turnId: 'aanya_turn_one',
            rawTranscript: 'Hello Anna',
            normalizedTranscript: 'Hello Aanya',
            response: 'Hello. It is lovely to hear from you.',
          ),
        );
        await _pumpAsyncWork(tester);

        expect(find.textContaining('speaking'), findsOneWidget);
        expect(find.text('Hello Aanya'), findsOneWidget);
        expect(find.text('Hello Anna'), findsNothing);
        expect(
          find.text('Hello. It is lovely to hear from you.'),
          findsOneWidget,
        );
        expect(harness.playback.playedUris, hasLength(1));

        harness.playback.completeNext();
        await _pumpAsyncWork(tester);
        await tester.pump(const Duration(milliseconds: 300));
        expect(find.text('Hold to talk'), findsWidgets);

        final secondGesture = await tester.startGesture(
          tester.getCenter(find.byKey(const Key('aanya_microphone_control'))),
        );
        await _pumpAsyncWork(tester);
        await secondGesture.up();
        await _pumpAsyncWork(tester);

        secondResponse.complete(
          _response(
            turnId: 'aanya_turn_two',
            rawTranscript: 'Anya tell me something calm',
            normalizedTranscript: 'Aanya tell me something calm',
            response: 'Take one slow breath. There is no need to rush.',
          ),
        );
        await _pumpAsyncWork(tester);

        expect(find.byKey(const Key('voice_session_history')), findsOneWidget);
        expect(
          find.byKey(const ValueKey('turn-user-aanya_turn_one')),
          findsOneWidget,
        );
        expect(
          find.byKey(const ValueKey('turn-aanya-aanya_turn_one')),
          findsOneWidget,
        );
        expect(
          find.byKey(const ValueKey('turn-user-aanya_turn_two')),
          findsOneWidget,
        );
        expect(
          find.byKey(const ValueKey('turn-aanya-aanya_turn_two')),
          findsOneWidget,
        );
        expect(find.text('Aanya tell me something calm'), findsOneWidget);
        expect(
          find.text('Take one slow breath. There is no need to rush.'),
          findsOneWidget,
        );
        expect(api.turnAudioPaths, hasLength(2));

        harness.playback.completeNext();
        await _pumpAsyncWork(tester);
        await _disposeVoiceScreen(tester, harness.controller);
      },
    );

    testWidgets('shows a useful error and lets the user recover', (
      tester,
    ) async {
      _usePhoneViewport(tester);
      final api = _FakeConversationApi()
        ..enqueueTurn((_) async {
          throw const ConversationApiException(
            ConversationApiErrorKind.processingFailure,
          );
        });
      final harness = _VoiceHarness(api: api);

      await _pumpVoiceScreen(tester, harness.controller);
      await _pumpAsyncWork(tester);

      final gesture = await tester.startGesture(
        tester.getCenter(find.byKey(const Key('aanya_microphone_control'))),
      );
      await _pumpAsyncWork(tester);
      await gesture.up();
      await _pumpAsyncWork(tester);

      expect(find.byKey(const Key('voice_error_message')), findsOneWidget);
      expect(
        find.text(
          "The local AI couldn't finish that response. Please try again.",
        ),
        findsOneWidget,
      );
      expect(find.byKey(const Key('clear_voice_error')), findsOneWidget);
      expect(harness.recordingStore.deletedPaths, hasLength(1));

      await tester.tap(find.byKey(const Key('clear_voice_error')));
      await tester.pump();

      expect(find.byKey(const Key('voice_error_message')), findsNothing);
      expect(find.text('Hold to talk'), findsWidgets);

      await _disposeVoiceScreen(tester, harness.controller);
    });
  });

  group('companion voice routing', () {
    testWidgets('Aanya uses the injected live voice builder', (tester) async {
      _usePhoneViewport(tester);
      var liveBuilderCalls = 0;
      Companion? routedCompanion;

      await _pumpCatalogApp(
        tester,
        liveBuilder: ({required companion, required onEnd}) {
          liveBuilderCalls += 1;
          routedCompanion = companion;
          return Scaffold(
            key: const Key('injected_aanya_live_call'),
            body: TextButton(
              key: const Key('end_injected_aanya_call'),
              onPressed: onEnd,
              child: const Text('End live Aanya call'),
            ),
          );
        },
      );

      await _openCallFor(tester, 'aanya');

      expect(find.byKey(const Key('injected_aanya_live_call')), findsOneWidget);
      expect(find.byType(MockVoiceCallScreen), findsNothing);
      expect(liveBuilderCalls, 1);
      expect(routedCompanion?.id, 'aanya');

      await tester.tap(find.byKey(const Key('end_injected_aanya_call')));
      await _pumpRoute(tester);
      expect(find.text('Companion profile'), findsOneWidget);
    });

    testWidgets('Tara stays on the mock route and never uses Aanya builder', (
      tester,
    ) async {
      _usePhoneViewport(tester);
      var liveBuilderCalls = 0;

      await _pumpCatalogApp(
        tester,
        liveBuilder: ({required companion, required onEnd}) {
          liveBuilderCalls += 1;
          return const SizedBox.shrink();
        },
      );

      await _openCallFor(tester, 'tara');

      expect(find.byType(MockVoiceCallScreen), findsOneWidget);
      expect(find.text('Mock voice call with Tara'), findsOneWidget);
      expect(find.byKey(const Key('injected_aanya_live_call')), findsNothing);
      expect(liveBuilderCalls, 0);

      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    });
  });
}

Future<void> _pumpVoiceScreen(
  WidgetTester tester,
  AanyaVoiceCallController controller,
) async {
  await tester.pumpWidget(
    DefaultAssetBundle(
      bundle: _PortraitStubAssetBundle(),
      child: MaterialApp(
        theme: ThemeData(useMaterial3: true),
        home: AanyaVoiceCallScreen(
          companion: _aanya,
          onEnd: () {},
          controller: controller,
        ),
      ),
    ),
  );
  await tester.pump();
}

Future<void> _disposeVoiceScreen(
  WidgetTester tester,
  AanyaVoiceCallController controller,
) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
  await controller.shutdownComplete;
}

Future<void> _pumpCatalogApp(
  WidgetTester tester, {
  required AanyaVoiceCallBuilder liveBuilder,
}) async {
  await tester.pumpWidget(
    DefaultAssetBundle(
      bundle: _PortraitStubAssetBundle(),
      child: AiraApp(
        initiallyOnboarded: true,
        companionCatalog: const _TwoCompanionCatalog(),
        aanyaVoiceCallBuilder: liveBuilder,
      ),
    ),
  );
  await _pumpAsyncWork(tester);
}

Future<void> _openCallFor(WidgetTester tester, String companionId) async {
  await tester.tap(find.byKey(ValueKey<String>('companion-card-$companionId')));
  await _pumpRoute(tester);
  expect(find.text('Companion profile'), findsOneWidget);

  await tester.tap(find.byKey(const Key('start-voice-call')));
  await _pumpRoute(tester);
}

Future<void> _pumpRoute(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 350));
}

Future<void> _pumpAsyncWork(WidgetTester tester) async {
  await tester.pump();
  await tester.pump();
  await tester.pump();
}

void _usePhoneViewport(WidgetTester tester) {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = const Size(480, 840);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
}

ConversationTurnResponse _response({
  required String turnId,
  required String rawTranscript,
  required String normalizedTranscript,
  required String response,
}) {
  final audioUrl = '/v1/conversation/turns/$turnId/audio';
  return ConversationTurnResponse(
    aiDisclosure: 'Aanya is a fictional AI companion.',
    turnId: turnId,
    companion: 'aanya',
    rawTranscript: rawTranscript,
    normalizedTranscript: normalizedTranscript,
    response: response,
    audioUrl: audioUrl,
    audioUri: Uri.parse('http://127.0.0.1:8765$audioUrl'),
  );
}

final class _VoiceHarness {
  _VoiceHarness({required this.api})
    : recorder = _FakeVoiceRecorder(),
      playback = _ControlledVoicePlayback(),
      recordingStore = _FakeRecordingStore() {
    controller = AanyaVoiceCallController(
      companionId: 'aanya',
      api: api,
      recorder: recorder,
      playback: playback,
      recordingStore: recordingStore,
      minimumRecordingDuration: Duration.zero,
    );
  }

  final _FakeConversationApi api;
  final _FakeVoiceRecorder recorder;
  final _ControlledVoicePlayback playback;
  final _FakeRecordingStore recordingStore;
  late final AanyaVoiceCallController controller;
}

final class _FakeConversationApi implements ConversationApi {
  _FakeConversationApi({Future<bool> Function()? healthCheck})
    : _healthCheck = healthCheck ?? (() async => true);

  final Future<bool> Function() _healthCheck;
  final List<Future<ConversationTurnResponse> Function(String)> _turnPlans = [];
  final List<String> turnAudioPaths = [];
  var healthCheckCount = 0;
  var disposed = false;

  void enqueueTurn(Future<ConversationTurnResponse> Function(String) plan) {
    _turnPlans.add(plan);
  }

  @override
  Future<bool> checkHealth() {
    healthCheckCount += 1;
    return _healthCheck();
  }

  @override
  Future<ConversationTurnResponse> createAanyaTurn(String audioPath) {
    turnAudioPaths.add(audioPath);
    if (_turnPlans.isEmpty) {
      throw StateError('No fake conversation response was queued.');
    }
    return _turnPlans.removeAt(0)(audioPath);
  }

  @override
  void dispose() {
    disposed = true;
  }
}

final class _FakeVoiceRecorder implements VoiceRecorder {
  var permissionGranted = true;
  var startCount = 0;
  var stopCount = 0;
  var cancelCount = 0;
  var disposeCount = 0;
  String? _activePath;

  @override
  Future<bool> hasPermission() async => permissionGranted;

  @override
  Future<void> startWav(String outputPath) async {
    startCount += 1;
    _activePath = outputPath;
  }

  @override
  Future<String?> stop() async {
    stopCount += 1;
    final path = _activePath;
    _activePath = null;
    return path;
  }

  @override
  Future<void> cancel() async {
    cancelCount += 1;
    _activePath = null;
  }

  @override
  Future<void> dispose() async {
    disposeCount += 1;
  }
}

final class _ControlledVoicePlayback implements VoicePlayback {
  final List<Uri> playedUris = [];
  final List<Completer<void>> _pendingPlayback = [];
  var stopCount = 0;
  var disposeCount = 0;

  @override
  Future<void> play(
    Uri audioUri, {
    required void Function() onPlaybackStarted,
  }) async {
    playedUris.add(audioUri);
    final completion = Completer<void>();
    _pendingPlayback.add(completion);
    onPlaybackStarted();
    await completion.future;
  }

  void completeNext() {
    final completion = _pendingPlayback.removeAt(0);
    if (!completion.isCompleted) completion.complete();
  }

  @override
  Future<void> stop() async {
    stopCount += 1;
    for (final completion in _pendingPlayback) {
      if (!completion.isCompleted) completion.complete();
    }
    _pendingPlayback.clear();
  }

  @override
  Future<void> dispose() async {
    disposeCount += 1;
  }
}

final class _FakeRecordingStore implements TemporaryRecordingStore {
  final List<String> deletedPaths = [];
  var _nextPath = 0;
  var disposeCount = 0;

  @override
  Future<String> allocateWavPath() async {
    _nextPath += 1;
    return '/tmp/aanya_widget_recording_$_nextPath.wav';
  }

  @override
  Future<void> delete(String path) async {
    deletedPaths.add(path);
  }

  @override
  Future<void> dispose() async {
    disposeCount += 1;
  }
}

final class _TwoCompanionCatalog implements CompanionCatalog {
  const _TwoCompanionCatalog();

  @override
  Future<List<Companion>> load() {
    return SynchronousFuture<List<Companion>>(<Companion>[_aanya, _tara]);
  }
}

final Companion _aanya = Companion(
  id: 'aanya',
  name: 'Aanya',
  personality: 'Gentle listener',
  tagline: 'Soft, patient, and calm',
  assetPath: 'assets/companions/aanya_test.png',
  accentColor: '#8C78C1',
  aiCompanion: true,
  adult: true,
  description: 'A clearly disclosed fictional AI conversation style.',
  filters: const <CompanionFilter>[CompanionFilter.gentle],
);

final Companion _tara = Companion(
  id: 'tara',
  name: 'Tara',
  personality: 'Practical and direct',
  tagline: 'Clear answers with honest warmth',
  assetPath: 'assets/companions/tara_test.png',
  accentColor: '#678B78',
  aiCompanion: true,
  adult: true,
  description: 'A clearly disclosed fictional AI conversation style.',
  filters: const <CompanionFilter>[CompanionFilter.practical],
);

/// Keeps the widget tests independent of production portrait decoding.
final class _PortraitStubAssetBundle extends CachingAssetBundle {
  static final ByteData _transparentPixel = ByteData.sublistView(
    base64Decode(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
      '+A8AAQUBAScY42YAAAAASUVORK5CYII=',
    ),
  );

  @override
  Future<ByteData> load(String key) {
    if (key.startsWith('assets/companions/')) {
      return SynchronousFuture<ByteData>(_transparentPixel);
    }
    return rootBundle.load(key);
  }
}
