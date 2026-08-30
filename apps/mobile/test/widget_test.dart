import 'dart:convert';

import 'package:female_voice_ai/app/aira_app.dart';
import 'package:female_voice_ai/features/companions/data/companion_catalog.dart';
import 'package:female_voice_ai/features/companions/domain/companion.dart';
import 'package:female_voice_ai/features/companions/domain/companion_filter.dart';
import 'package:female_voice_ai/features/companions/presentation/companion_grid_screen.dart';
import 'package:female_voice_ai/features/settings/settings_screen.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('onboarding gates the fictional AI catalog behind 18+', (
    tester,
  ) async {
    _useAndroidPhoneViewport(tester);
    await _pumpAiraApp(tester);

    expect(
      find.textContaining('Every companion in Aira is a fictional AI'),
      findsOneWidget,
    );
    expect(
      find.textContaining('not real people, therapists, or emergency services'),
      findsOneWidget,
    );

    FilledButton continueButton() => tester.widget<FilledButton>(
      find.byKey(const Key('onboarding_continue_button')),
    );

    expect(continueButton().onPressed, isNull);
    await _tapAfterScrolling(
      tester,
      find.byKey(const Key('onboarding_ai_confirmation')),
    );
    expect(continueButton().onPressed, isNull);

    await _tapAfterScrolling(
      tester,
      find.byKey(const Key('onboarding_age_confirmation')),
    );
    expect(continueButton().onPressed, isNotNull);

    await _tapAfterScrolling(
      tester,
      find.byKey(const Key('onboarding_continue_button')),
      waitForCatalog: true,
    );

    expect(find.text('Choose your conversation style'), findsOneWidget);
    expect(find.text('20 AI companions'), findsOneWidget);
    expect(find.byType(NavigationBar), findsOneWidget);

    final catalogContext = tester.element(find.byType(CompanionGridScreen));
    expect(Theme.of(catalogContext).brightness, Brightness.dark);
  });

  testWidgets('onboarding disclosure reflows at large Android text', (
    tester,
  ) async {
    _useAndroidPhoneViewport(
      tester,
      size: const Size(320, 568),
      textScaleFactor: 2,
    );
    await _pumpAiraApp(tester);

    expect(find.text('Fictional AI companions'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'filters, details, and mock call stay local and companion-aware',
    (tester) async {
      _useAndroidPhoneViewport(tester, size: const Size(320, 568));
      await _pumpAiraApp(tester, initiallyOnboarded: true);

      final catalogScrollable = _catalogScrollable();
      final aanyaCard = find.byKey(const ValueKey('companion-card-aanya'));
      await tester.scrollUntilVisible(
        aanyaCard,
        240,
        scrollable: catalogScrollable,
      );
      await tester.pump();
      expect(aanyaCard, findsOneWidget);
      expect(
        find.descendant(of: aanyaCard, matching: find.text('Aanya')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: aanyaCard, matching: find.text('Gentle listener')),
        findsOneWidget,
      );
      expect(
        find.descendant(
          of: aanyaCard,
          matching: find.text('Soft, patient, and here to listen'),
        ),
        findsOneWidget,
      );
      expect(
        find.descendant(of: aanyaCard, matching: find.text('AI')),
        findsOneWidget,
      );

      final gentleFilter = find.byKey(const ValueKey('filter-Gentle'));
      await tester.scrollUntilVisible(
        gentleFilter,
        -240,
        scrollable: catalogScrollable,
      );
      await tester.tap(gentleFilter);
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.text('4 AI companions'), findsOneWidget);
      expect(find.byKey(const ValueKey('companion-card-tara')), findsNothing);

      await tester.scrollUntilVisible(
        aanyaCard,
        240,
        scrollable: catalogScrollable,
      );
      await tester.tap(aanyaCard);
      await _pumpRouteTransition(tester);

      expect(find.text('Companion profile'), findsOneWidget);
      expect(find.text('Personality'), findsOneWidget);
      expect(find.text('Memory'), findsOneWidget);
      expect(find.text('AI disclosure'), findsOneWidget);
      expect(
        find.textContaining('fictional AI companion. It is not a person'),
        findsOneWidget,
      );

      await tester.tap(find.byKey(const Key('start-voice-call')));
      await _pumpRouteTransition(tester);

      expect(find.text('Mock voice call with Aanya'), findsOneWidget);
      expect(find.text('Fictional AI'), findsWidgets);
      expect(find.byKey(const Key('call_mute_button')), findsOneWidget);
      expect(find.byKey(const Key('call_speaker_button')), findsOneWidget);
      expect(find.byKey(const Key('call_end_button')), findsOneWidget);

      final speakerButton = find.byKey(const Key('call_speaker_button'));
      await tester.ensureVisible(speakerButton);
      await tester.tap(speakerButton);
      await tester.pump();
      expect(find.text('Speaker off'), findsOneWidget);

      final muteButton = find.byKey(const Key('call_mute_button'));
      await tester.ensureVisible(muteButton);
      await tester.tap(muteButton);
      await tester.pump();
      expect(find.text('Muted'), findsOneWidget);

      await tester.pump(const Duration(seconds: 1));
      expect(find.text('00:01'), findsOneWidget);

      final endButton = find.byKey(const Key('call_end_button'));
      await tester.ensureVisible(endButton);
      await tester.tap(endButton);
      await _pumpRouteTransition(tester);
      expect(find.text('Companion profile'), findsOneWidget);
    },
  );

  testWidgets('catalog grid can reach the twentieth companion', (tester) async {
    _useAndroidPhoneViewport(tester);
    await _pumpAiraApp(tester, initiallyOnboarded: true);

    final rheaCard = find.byKey(const ValueKey('companion-card-rhea'));
    await tester.scrollUntilVisible(
      rheaCard,
      600,
      scrollable: _catalogScrollable(),
    );
    await tester.pump();

    expect(rheaCard, findsOneWidget);
    expect(
      find.descendant(of: rheaCard, matching: find.text('Rhea')),
      findsOneWidget,
    );
    expect(
      find.descendant(of: rheaCard, matching: find.text('AI')),
      findsOneWidget,
    );
  });

  testWidgets('schedule selection remains deterministic and local', (
    tester,
  ) async {
    _useAndroidPhoneViewport(tester);
    await _pumpAiraApp(tester, initiallyOnboarded: true);

    await tester.tap(find.byIcon(Icons.calendar_today_outlined));
    await tester.pump();

    expect(find.text('Select one or more days'), findsOneWidget);
    await tester.tap(find.text('Sat'));
    await tester.pump();

    final saveButton = find.widgetWithText(FilledButton, 'Save schedule');
    await tester.ensureVisible(saveButton);
    await tester.tap(saveButton);
    await tester.pump();

    expect(find.text('Conversation schedule saved locally.'), findsOneWidget);
  });

  testWidgets('memory needs consent and appearance can return to dark', (
    tester,
  ) async {
    _useAndroidPhoneViewport(tester);
    await _pumpAiraApp(tester, initiallyOnboarded: true);

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pump();

    final memorySwitch = find.byType(SwitchListTile);
    expect(tester.widget<SwitchListTile>(memorySwitch).value, isFalse);
    await tester.ensureVisible(memorySwitch);
    await tester.tap(memorySwitch);
    await _pumpFiniteAnimation(tester);

    expect(find.text('Allow long-term memory?'), findsOneWidget);
    await tester.tap(find.text('Not now'));
    await _pumpFiniteAnimation(tester);
    expect(tester.widget<SwitchListTile>(memorySwitch).value, isFalse);

    await tester.tap(memorySwitch);
    await _pumpFiniteAnimation(tester);
    await tester.tap(find.text('I understand, enable'));
    await _pumpFiniteAnimation(tester);
    expect(tester.widget<SwitchListTile>(memorySwitch).value, isTrue);

    final lightChoice = find.widgetWithText(ChoiceChip, 'Light');
    await tester.ensureVisible(lightChoice);
    await tester.tap(lightChoice);
    await _pumpFiniteAnimation(tester);
    var settingsContext = tester.element(find.byType(SettingsScreen));
    expect(Theme.of(settingsContext).brightness, Brightness.light);

    final darkChoice = find.widgetWithText(ChoiceChip, 'Dark');
    await tester.ensureVisible(darkChoice);
    await tester.tap(darkChoice);
    await _pumpFiniteAnimation(tester);
    settingsContext = tester.element(find.byType(SettingsScreen));
    expect(Theme.of(settingsContext).brightness, Brightness.dark);
  });
}

Future<void> _pumpAiraApp(
  WidgetTester tester, {
  bool initiallyOnboarded = false,
}) async {
  await tester.pumpWidget(
    DefaultAssetBundle(
      bundle: _PortraitStubAssetBundle(),
      child: AiraApp(
        initiallyOnboarded: initiallyOnboarded,
        companionCatalog: const _FixtureCompanionCatalog(),
      ),
    ),
  );
  await tester.pump();

  if (initiallyOnboarded) {
    _expectCatalogReady();
  }
}

void _expectCatalogReady() {
  expect(
    find.byKey(const PageStorageKey<String>('companion-catalog-ready')),
    findsOneWidget,
  );
}

Finder _catalogScrollable() {
  return find
      .descendant(
        of: find.byType(CompanionGridScreen),
        matching: find.byWidgetPredicate(
          (widget) =>
              widget is Scrollable &&
              widget.axisDirection == AxisDirection.down,
        ),
      )
      .first;
}

void _useAndroidPhoneViewport(
  WidgetTester tester, {
  Size size = const Size(360, 800),
  double textScaleFactor = 1,
}) {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  tester.platformDispatcher.textScaleFactorTestValue = textScaleFactor;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
}

Future<void> _tapAfterScrolling(
  WidgetTester tester,
  Finder finder, {
  bool waitForCatalog = false,
}) async {
  await tester.ensureVisible(finder);
  await tester.tap(finder);
  await tester.pump();

  if (waitForCatalog) {
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pump();
    _expectCatalogReady();
  } else {
    await tester.pump(const Duration(milliseconds: 250));
  }
}

Future<void> _pumpRouteTransition(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 350));
}

Future<void> _pumpFiniteAnimation(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

/// Avoids decoding twenty multi-megabyte production portraits in widget tests.
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

final class _FixtureCompanionCatalog implements CompanionCatalog {
  const _FixtureCompanionCatalog();

  @override
  Future<List<Companion>> load() {
    return SynchronousFuture<List<Companion>>(_fixtureCompanions);
  }
}

final List<Companion> _fixtureCompanions = List<Companion>.unmodifiable(
  _companionFixtures.asMap().entries.map((entry) {
    final fixture = entry.value;
    return Companion(
      id: fixture.id,
      name: fixture.name,
      personality: fixture.personality,
      tagline: fixture.tagline,
      assetPath: 'assets/companions/fixture_${entry.key + 1}.png',
      accentColor: '#8C78C1',
      aiCompanion: true,
      adult: true,
      description:
          '${fixture.name} uses a clearly disclosed fictional AI conversation '
          'style designed for this deterministic local preview.',
      filters: fixture.filters,
    );
  }),
);

final class _CompanionFixture {
  const _CompanionFixture(
    this.id,
    this.name,
    this.personality,
    this.tagline,
    this.filters,
  );

  final String id;
  final String name;
  final String personality;
  final String tagline;
  final List<CompanionFilter> filters;
}

const List<_CompanionFixture> _companionFixtures = [
  _CompanionFixture(
    'aanya',
    'Aanya',
    'Gentle listener',
    'Soft, patient, and here to listen',
    [CompanionFilter.gentle, CompanionFilter.calm],
  ),
  _CompanionFixture(
    'tara',
    'Tara',
    'Practical & direct',
    'Clear answers with honest warmth',
    [CompanionFilter.practical, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'riya',
    'Riya',
    'Cheerful & energetic',
    'Bright energy for a lighter day',
    [CompanionFilter.fun, CompanionFilter.motivating],
  ),
  _CompanionFixture(
    'kavya',
    'Kavya',
    'Bookish & thoughtful',
    'Curious conversations that go deeper',
    [CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'naina',
    'Naina',
    'Playful & witty',
    'Quick banter and an easy smile',
    [CompanionFilter.fun],
  ),
  _CompanionFixture(
    'isha',
    'Isha',
    'Calm & mindful',
    'A peaceful pause when life feels loud',
    [CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'sana',
    'Sana',
    'Ambitious motivator',
    'Focused encouragement for your next move',
    [CompanionFilter.practical, CompanionFilter.motivating],
  ),
  _CompanionFixture(
    'diya',
    'Diya',
    'Artistic & creative',
    'Imaginative company for curious minds',
    [CompanionFilter.fun, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'anika',
    'Anika',
    'Adventurous spirit',
    'Fresh stories and spontaneous ideas',
    [CompanionFilter.fun, CompanionFilter.motivating],
  ),
  _CompanionFixture(
    'meera',
    'Meera',
    'Mature & grounded',
    'Steady perspective without judgment',
    [
      CompanionFilter.practical,
      CompanionFilter.calm,
      CompanionFilter.thoughtful,
    ],
  ),
  _CompanionFixture(
    'zara',
    'Zara',
    'Tech-savvy thinker',
    'Smart curiosity with a modern edge',
    [CompanionFilter.practical, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'priya',
    'Priya',
    'Warmly empathetic',
    'Thoughtful support for difficult moments',
    [CompanionFilter.gentle, CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'leela',
    'Leela',
    'Traditional & warm',
    'Familiar warmth and heartfelt conversation',
    [CompanionFilter.gentle, CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'maya',
    'Maya',
    'Modern & confident',
    'Bold perspective with calm confidence',
    [
      CompanionFilter.fun,
      CompanionFilter.practical,
      CompanionFilter.motivating,
    ],
  ),
  _CompanionFixture(
    'simran',
    'Simran',
    'Fitness-minded',
    'Upbeat accountability for healthy routines',
    [CompanionFilter.practical, CompanionFilter.motivating],
  ),
  _CompanionFixture(
    'noor',
    'Noor',
    'Soothing & patient',
    'Gentle company at your own pace',
    [CompanionFilter.gentle, CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'avni',
    'Avni',
    'Analytical problem-solver',
    'Calm thinking for complicated days',
    [
      CompanionFilter.practical,
      CompanionFilter.calm,
      CompanionFilter.thoughtful,
    ],
  ),
  _CompanionFixture(
    'myra',
    'Myra',
    'Humorous & lighthearted',
    'Light laughs and playful conversation',
    [CompanionFilter.fun],
  ),
  _CompanionFixture(
    'saanvi',
    'Saanvi',
    'Musical & soulful',
    'Emotion, music, and meaningful talks',
    [CompanionFilter.fun, CompanionFilter.calm, CompanionFilter.thoughtful],
  ),
  _CompanionFixture(
    'rhea',
    'Rhea',
    'Independent & straightforward',
    'Direct conversation with no pretence',
    [CompanionFilter.practical, CompanionFilter.motivating],
  ),
];
