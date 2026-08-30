import 'dart:ui';

import 'companion_filter.dart';

/// Immutable, validated metadata for one fictional AI companion.
final class Companion {
  factory Companion({
    required String id,
    required String name,
    required String personality,
    required String tagline,
    required String assetPath,
    required String accentColor,
    required bool aiCompanion,
    required bool adult,
    required String description,
    required List<CompanionFilter> filters,
  }) {
    final validatedId = _validateId(id);
    final validatedName = _validateRequiredText(name, 'name');
    final validatedPersonality = _validateRequiredText(
      personality,
      'personality',
    );
    final validatedTagline = _validateRequiredText(tagline, 'tagline');
    final validatedAssetPath = _validateAssetPath(assetPath);
    final validatedAccentColor = _validateAccentColor(accentColor);
    final validatedDescription = _validateRequiredText(
      description,
      'description',
    );
    final validatedFilters = _validateFilters(filters);

    if (!aiCompanion) {
      throw const FormatException(
        'Companion entries must explicitly set aiCompanion to true.',
      );
    }
    if (!adult) {
      throw const FormatException(
        'Companion entries must explicitly set adult to true.',
      );
    }

    return Companion._(
      id: validatedId,
      name: validatedName,
      personality: validatedPersonality,
      tagline: validatedTagline,
      assetPath: validatedAssetPath,
      accentColor: validatedAccentColor,
      aiCompanion: aiCompanion,
      adult: adult,
      description: validatedDescription,
      filters: validatedFilters,
    );
  }

  const Companion._({
    required this.id,
    required this.name,
    required this.personality,
    required this.tagline,
    required this.assetPath,
    required this.accentColor,
    required this.aiCompanion,
    required this.adult,
    required this.description,
    required this.filters,
  });

  /// Parses and validates one object from the bundled catalog.
  factory Companion.fromJson(Map<String, Object?> json) {
    return Companion(
      id: _readRequiredString(json, 'id'),
      name: _readRequiredString(json, 'name'),
      personality: _readRequiredString(json, 'personality'),
      tagline: _readRequiredString(json, 'tagline'),
      assetPath: _readRequiredString(json, 'assetPath'),
      accentColor: _readRequiredString(json, 'accentColor'),
      aiCompanion: _readRequiredBool(json, 'aiCompanion'),
      adult: _readRequiredBool(json, 'adult'),
      description: _readRequiredString(json, 'description'),
      filters: _readFilters(json),
    );
  }

  final String id;
  final String name;
  final String personality;
  final String tagline;
  final String assetPath;

  /// The normalized catalog color in `#RRGGBB` form.
  final String accentColor;

  /// Always true for a successfully constructed catalog entry.
  final bool aiCompanion;

  /// Always true for a successfully constructed catalog entry.
  final bool adult;

  final String description;
  final List<CompanionFilter> filters;

  /// The catalog accent converted to an opaque Flutter [Color].
  Color get accent {
    final rgb = int.parse(accentColor.substring(1), radix: 16);
    return Color(0xFF000000 | rgb);
  }

  /// Whether this companion is included in [filter].
  bool matchesFilter(CompanionFilter filter) => filters.contains(filter);

  Map<String, Object> toJson() => <String, Object>{
    'id': id,
    'name': name,
    'personality': personality,
    'tagline': tagline,
    'assetPath': assetPath,
    'accentColor': accentColor,
    'aiCompanion': aiCompanion,
    'adult': adult,
    'description': description,
    'filters': filters.map((filter) => filter.toJson()).toList(growable: false),
  };

  static String _readRequiredString(
    Map<String, Object?> json,
    String fieldName,
  ) {
    if (!json.containsKey(fieldName)) {
      throw FormatException('Missing required companion field "$fieldName".');
    }

    final value = json[fieldName];
    if (value is! String) {
      throw FormatException(
        'Companion field "$fieldName" must be a string; received '
        '${value.runtimeType}.',
      );
    }
    return value;
  }

  static bool _readRequiredBool(Map<String, Object?> json, String fieldName) {
    if (!json.containsKey(fieldName)) {
      throw FormatException('Missing required companion field "$fieldName".');
    }

    final value = json[fieldName];
    if (value is! bool) {
      throw FormatException(
        'Companion field "$fieldName" must be a boolean; received '
        '${value.runtimeType}.',
      );
    }
    return value;
  }

  static List<CompanionFilter> _readFilters(Map<String, Object?> json) {
    const fieldName = 'filters';
    if (!json.containsKey(fieldName)) {
      throw const FormatException(
        'Missing required companion field "filters".',
      );
    }

    final value = json[fieldName];
    if (value is! List<Object?>) {
      throw FormatException(
        'Companion field "filters" must be a list; received '
        '${value.runtimeType}.',
      );
    }

    return value.map(CompanionFilter.fromJson).toList(growable: false);
  }

  static String _validateRequiredText(String value, String fieldName) {
    final normalizedValue = value.trim();
    if (normalizedValue.isEmpty) {
      throw FormatException('Companion field "$fieldName" cannot be empty.');
    }
    return normalizedValue;
  }

  static String _validateId(String value) {
    final normalizedValue = _validateRequiredText(value, 'id');
    if (!RegExp(r'^[a-z0-9]+(?:[-_][a-z0-9]+)*$').hasMatch(normalizedValue)) {
      throw const FormatException(
        'Companion field "id" must use lowercase letters, numbers, hyphens, '
        'or underscores.',
      );
    }
    return normalizedValue;
  }

  static String _validateAssetPath(String value) {
    final normalizedValue = _validateRequiredText(value, 'assetPath');
    final localPortraitPattern = RegExp(
      r'^assets/companions/[A-Za-z0-9._-]+\.(?:png|jpe?g|webp)$',
    );
    if (!localPortraitPattern.hasMatch(normalizedValue)) {
      throw const FormatException(
        'Companion field "assetPath" must reference a local image under '
        'assets/companions/.',
      );
    }
    return normalizedValue;
  }

  static String _validateAccentColor(String value) {
    final normalizedValue = value.trim().toUpperCase();
    if (!RegExp(r'^#[0-9A-F]{6}$').hasMatch(normalizedValue)) {
      throw const FormatException(
        'Companion field "accentColor" must be a #RRGGBB hex color.',
      );
    }
    return normalizedValue;
  }

  static List<CompanionFilter> _validateFilters(List<CompanionFilter> filters) {
    if (filters.isEmpty) {
      throw const FormatException(
        'Companion field "filters" must contain at least one filter.',
      );
    }

    final uniqueFilters = <CompanionFilter>{};
    for (final filter in filters) {
      if (!uniqueFilters.add(filter)) {
        throw FormatException(
          'Companion field "filters" contains duplicate value '
          '"${filter.label}".',
        );
      }
    }
    return List<CompanionFilter>.unmodifiable(filters);
  }
}
