import 'dart:convert';

import 'package:flutter/services.dart';

import '../domain/companion.dart';
import 'companion_catalog.dart';

/// Loads the deterministic companion catalog bundled with the Flutter app.
final class LocalCompanionCatalog implements CompanionCatalog {
  LocalCompanionCatalog({
    AssetBundle? bundle,
    this.assetPath = defaultAssetPath,
  }) : _bundle = bundle ?? rootBundle {
    if (assetPath.trim().isEmpty) {
      throw ArgumentError.value(assetPath, 'assetPath', 'Must not be empty.');
    }
  }

  static const String defaultAssetPath = 'assets/data/companions.json';

  final AssetBundle _bundle;
  final String assetPath;

  /// Loads, validates, and returns catalog entries in their source order.
  ///
  /// The returned list is unmodifiable. A [FormatException] is thrown when
  /// the JSON root, an entry, or a companion field is invalid, or when IDs are
  /// duplicated.
  @override
  Future<List<Companion>> load() async {
    final source = await _bundle.loadString(assetPath);

    final Object? decoded;
    try {
      decoded = jsonDecode(source);
    } on FormatException catch (error) {
      throw FormatException(
        'Invalid JSON in companion catalog "$assetPath": ${error.message}',
      );
    }

    if (decoded is! List<Object?>) {
      throw FormatException(
        'Companion catalog "$assetPath" must contain a JSON array.',
      );
    }

    final companions = <Companion>[];
    final companionIds = <String>{};

    for (var index = 0; index < decoded.length; index += 1) {
      final entry = decoded[index];
      if (entry is! Map<String, Object?>) {
        throw FormatException(
          'Companion catalog entry at index $index must be a JSON object.',
        );
      }

      final Companion companion;
      try {
        companion = Companion.fromJson(entry);
      } on FormatException catch (error) {
        throw FormatException(
          'Invalid companion at index $index: ${error.message}',
        );
      }

      if (!companionIds.add(companion.id)) {
        throw FormatException(
          'Duplicate companion id "${companion.id}" at index $index.',
        );
      }
      companions.add(companion);
    }

    return List<Companion>.unmodifiable(companions);
  }
}
