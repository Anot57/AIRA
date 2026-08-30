/// The supported traits used to filter the local companion catalog.
enum CompanionFilter {
  gentle('Gentle'),
  fun('Fun'),
  practical('Practical'),
  motivating('Motivating'),
  calm('Calm'),
  thoughtful('Thoughtful');

  const CompanionFilter(this.label);

  /// The user-facing label displayed by filter chips.
  final String label;

  /// Parses the exact user-facing value stored in the local catalog.
  ///
  /// A [FormatException] is thrown for non-string values or unsupported
  /// filters so malformed catalog entries cannot silently enter app state.
  static CompanionFilter fromJson(Object? value) {
    if (value is! String) {
      throw FormatException(
        'Companion filters must be strings; received ${value.runtimeType}.',
      );
    }

    final normalizedValue = value.trim();
    for (final filter in values) {
      if (filter.label == normalizedValue) {
        return filter;
      }
    }

    final supportedValues = values.map((filter) => filter.label).join(', ');
    throw FormatException(
      'Unknown companion filter "$value". Supported filters: '
      '$supportedValues.',
    );
  }

  /// The catalog representation for this filter.
  String toJson() => label;
}
