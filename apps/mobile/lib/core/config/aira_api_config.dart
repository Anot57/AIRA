/// Central configuration for the trusted-local Aira voice API.
final class AiraApiConfig {
  AiraApiConfig.fromBaseUrl(String baseUrl) : baseUri = _parseBaseUri(baseUrl);

  factory AiraApiConfig() => AiraApiConfig.fromBaseUrl(_configuredBaseUrl);

  static const String environmentKey = 'AIRA_API_BASE_URL';
  static const String fallbackBaseUrl = 'http://192.168.1.100:8765';

  static const String _configuredBaseUrl = String.fromEnvironment(
    environmentKey,
    defaultValue: fallbackBaseUrl,
  );

  final Uri baseUri;

  String get normalizedBaseUrl => baseUri.toString();

  Uri get healthUri => endpoint('/health');

  Uri get readinessUri => endpoint('/ready');

  Uri get conversationTurnUri => endpoint('/v1/conversation/turn');

  /// Persistent realtime endpoint derived from the same trusted API origin.
  Uri get realtimeUri =>
      endpoint('/v1/realtime')
          .replace(scheme: baseUri.scheme == 'https' ? 'wss' : 'ws');

  /// Builds an API endpoint below [baseUri] without string concatenation.
  Uri endpoint(String path) {
    final value = path.trim();
    final rawPathSegments = value.split('/');
    if (rawPathSegments.any((segment) => segment == '.' || segment == '..')) {
      throw ArgumentError.value(path, 'path', 'Must be a safe API path.');
    }

    final candidate = Uri.tryParse(value);
    if (candidate == null ||
        candidate.hasScheme ||
        candidate.hasAuthority ||
        candidate.hasQuery ||
        candidate.hasFragment ||
        candidate.pathSegments.any(
          (segment) => segment == '.' || segment == '..',
        )) {
      throw ArgumentError.value(path, 'path', 'Must be a safe API path.');
    }

    final endpointPath = candidate.path.replaceFirst(RegExp(r'^/+'), '');
    if (endpointPath.isEmpty) {
      throw ArgumentError.value(path, 'path', 'Must not be empty.');
    }

    final basePath = baseUri.path.replaceFirst(RegExp(r'/+$'), '');
    return baseUri.replace(path: '$basePath/$endpointPath');
  }

  /// Resolves a backend-provided turn WAV URL and rejects every other origin
  /// and route shape.
  Uri resolveTurnAudioUrl(String audioUrl) {
    final value = audioUrl.trim();
    final candidate = Uri.tryParse(value);
    if (value.isEmpty ||
        candidate == null ||
        candidate.userInfo.isNotEmpty ||
        candidate.hasQuery ||
        candidate.hasFragment) {
      throw const FormatException('Invalid conversation audio URL.');
    }

    final origin = baseUri.replace(path: '/', query: null, fragment: null);
    final resolved = origin.resolveUri(candidate);
    if (!_hasSameOrigin(origin, resolved) ||
        !_turnAudioPath.hasMatch(resolved.path)) {
      throw const FormatException('Invalid conversation audio URL.');
    }
    return resolved;
  }

  static final RegExp _turnAudioPath = RegExp(
    r'^/v1/conversation/turns/[a-z0-9][a-z0-9_-]{0,119}/audio$',
  );

  static Uri _parseBaseUri(String rawValue) {
    final value = rawValue.trim();
    final uri = Uri.tryParse(value);
    if (value.isEmpty ||
        uri == null ||
        (uri.scheme != 'http' && uri.scheme != 'https') ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment) {
      throw const FormatException(
        'AIRA_API_BASE_URL must be an http or https origin.',
      );
    }

    var normalizedPath = uri.path;
    while (normalizedPath.endsWith('/')) {
      normalizedPath = normalizedPath.substring(0, normalizedPath.length - 1);
    }
    if (normalizedPath.isNotEmpty) {
      throw const FormatException(
        'AIRA_API_BASE_URL must be an http or https origin.',
      );
    }

    return uri.replace(
      scheme: uri.scheme.toLowerCase(),
      host: uri.host.toLowerCase(),
      path: '',
    );
  }

  static bool _hasSameOrigin(Uri expected, Uri actual) {
    return expected.scheme.toLowerCase() == actual.scheme.toLowerCase() &&
        expected.host.toLowerCase() == actual.host.toLowerCase() &&
        _effectivePort(expected) == _effectivePort(actual);
  }

  static int _effectivePort(Uri uri) {
    if (uri.hasPort) return uri.port;
    return uri.scheme == 'https' ? 443 : 80;
  }
}
