import '../domain/companion.dart';

/// Source of validated fictional AI companion profiles.
///
/// Production uses the bundled local catalog, while widget tests can provide a
/// deterministic in-memory implementation without touching Flutter assets.
abstract interface class CompanionCatalog {
  Future<List<Companion>> load();
}
