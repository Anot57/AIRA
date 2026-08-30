import 'package:female_voice_ai/features/companions/data/local_companion_catalog.dart';
import 'package:female_voice_ai/features/companions/domain/companion.dart';
import 'package:female_voice_ai/features/companions/domain/companion_filter.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'bundled catalog contains 20 safe records and registered portraits',
    () async {
      final companions = await LocalCompanionCatalog().load();

      expect(companions, hasLength(20));
      expect(companions.first.id, 'aanya');
      expect(companions.last.id, 'rhea');
      expect(companions.every((companion) => companion.aiCompanion), isTrue);
      expect(companions.every((companion) => companion.adult), isTrue);
      expect(
        companions.every((companion) => companion.filters.isNotEmpty),
        isTrue,
      );

      for (final filter in CompanionFilter.values) {
        expect(
          companions.any((companion) => companion.matchesFilter(filter)),
          isTrue,
          reason: '${filter.label} should include at least one companion.',
        );
      }

      final manifest = await AssetManifest.loadFromAssetBundle(rootBundle);
      final registeredPortraits = manifest
          .listAssets()
          .where((asset) => asset.startsWith('assets/companions/'))
          .toSet();
      final catalogPortraits = companions
          .map((companion) => companion.assetPath)
          .toSet();

      expect(registeredPortraits, hasLength(20));
      expect(registeredPortraits, catalogPortraits);
    },
  );

  test('companion parsing rejects a record not disclosed as AI', () async {
    final companions = await LocalCompanionCatalog().load();
    final unsafeRecord = Map<String, Object?>.of(companions.first.toJson())
      ..['aiCompanion'] = false;

    expect(
      () => Companion.fromJson(unsafeRecord),
      throwsA(isA<FormatException>()),
    );
  });
}
