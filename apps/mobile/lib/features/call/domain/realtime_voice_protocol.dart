import 'dart:convert';

/// Versioned wire contract shared by the Android realtime client and Aira's
/// trusted-local WebSocket service.
abstract final class AiraRealtimeProtocol {
  static const int version = 1;
  static const String companionId = 'aanya';
  static const String pcmEncoding = 'pcm_s16le';
  static const int sampleRateHz = 16000;
  static const int channels = 1;

  /// A small fixed ceiling prevents a peer from growing client memory without
  /// bound. Client microphone frames and server audio frames share this cap.
  static const int maxFrameBytes = 64 * 1024;
  static const int maxTurnAudioBytes = 2 * 1024 * 1024;
  static const int maxClientControlBytes = 16 * 1024;
  static const int maxServerControlBytes = 64 * 1024;
  static const int maxTextCharacters = 10000;
  static const int maxTextDeltaCharacters = 4096;
  static const int minPlaybackSampleRateHz = 8000;
  static const int maxPlaybackSampleRateHz = 48000;
}

final class RealtimeProtocolException implements Exception {
  const RealtimeProtocolException(this.code, this.message);

  final String code;
  final String message;

  @override
  String toString() => 'RealtimeProtocolException($code): $message';
}

final class RealtimeAudioFormat {
  const RealtimeAudioFormat({
    required this.encoding,
    required this.sampleRateHz,
    required this.channels,
  });

  const RealtimeAudioFormat.pcm16Mono16Khz()
    : encoding = AiraRealtimeProtocol.pcmEncoding,
      sampleRateHz = AiraRealtimeProtocol.sampleRateHz,
      channels = AiraRealtimeProtocol.channels;

  factory RealtimeAudioFormat.fromJson(Map<String, Object?> json) {
    final encoding = _requiredString(json, 'encoding', maxLength: 32);
    final sampleRateHz = _requiredInt(json, 'sample_rate_hz');
    final channels = _requiredInt(json, 'channels');
    if (encoding != AiraRealtimeProtocol.pcmEncoding ||
        sampleRateHz != AiraRealtimeProtocol.sampleRateHz ||
        channels != AiraRealtimeProtocol.channels) {
      throw const RealtimeProtocolException(
        'unsupported_audio_format',
        'The realtime service selected an unsupported audio format.',
      );
    }
    return RealtimeAudioFormat(
      encoding: encoding,
      sampleRateHz: sampleRateHz,
      channels: channels,
    );
  }

  /// Validates synthesized playback PCM. The microphone contract is fixed at
  /// 16 kHz, while a self-hosted synthesizer may return another safe mono rate
  /// (Qwen currently returns 24 kHz) and declares it per audio frame.
  factory RealtimeAudioFormat.fromPlaybackJson(Map<String, Object?> json) {
    final encoding = _requiredString(json, 'encoding', maxLength: 32);
    final sampleRateHz = _requiredInt(json, 'sample_rate_hz');
    final channels = _requiredInt(json, 'channels');
    if (encoding != AiraRealtimeProtocol.pcmEncoding ||
        sampleRateHz < AiraRealtimeProtocol.minPlaybackSampleRateHz ||
        sampleRateHz > AiraRealtimeProtocol.maxPlaybackSampleRateHz ||
        channels != 1) {
      throw const RealtimeProtocolException(
        'unsupported_audio_format',
        'The realtime service selected an unsupported playback audio format.',
      );
    }
    return RealtimeAudioFormat(
      encoding: encoding,
      sampleRateHz: sampleRateHz,
      channels: channels,
    );
  }

  final String encoding;
  final int sampleRateHz;
  final int channels;

  Map<String, Object?> toJson() => <String, Object?>{
    'encoding': encoding,
    'sample_rate_hz': sampleRateHz,
    'channels': channels,
  };
}

sealed class RealtimeClientMessage {
  const RealtimeClientMessage();

  String get type;

  Map<String, Object?> toJson();

  String toWireText() => jsonEncode(<String, Object?>{
    'type': type,
    'protocol_version': AiraRealtimeProtocol.version,
    ...toJson(),
  });
}

final class RealtimeSessionStartMessage extends RealtimeClientMessage {
  const RealtimeSessionStartMessage({
    this.companion = AiraRealtimeProtocol.companionId,
    this.audioFormat = const RealtimeAudioFormat.pcm16Mono16Khz(),
  });

  final String companion;
  final RealtimeAudioFormat audioFormat;

  @override
  String get type => 'session_start';

  @override
  Map<String, Object?> toJson() => <String, Object?>{
    'companion': companion,
    'audio_format': audioFormat.toJson(),
  };
}

final class RealtimeEndOfTurnMessage extends RealtimeClientMessage {
  const RealtimeEndOfTurnMessage();

  @override
  String get type => 'end_of_turn';

  @override
  Map<String, Object?> toJson() => const <String, Object?>{};
}

final class RealtimeCancelTurnMessage extends RealtimeClientMessage {
  const RealtimeCancelTurnMessage();

  @override
  String get type => 'cancel_turn';

  @override
  Map<String, Object?> toJson() => const <String, Object?>{};
}

final class RealtimeSessionEndMessage extends RealtimeClientMessage {
  const RealtimeSessionEndMessage();

  @override
  String get type => 'session_end';

  @override
  Map<String, Object?> toJson() => const <String, Object?>{};
}

sealed class RealtimeServerEvent {
  const RealtimeServerEvent({required this.protocolVersion});

  final int protocolVersion;
}

final class RealtimeReadiness {
  const RealtimeReadiness({
    required this.status,
    required this.components,
    required this.message,
  });

  factory RealtimeReadiness.fromJson(Map<String, Object?> json) {
    final status = _requiredString(json, 'status', maxLength: 32);
    if (!_readinessStatuses.contains(status)) {
      throw const RealtimeProtocolException(
        'invalid_readiness',
        'The realtime service sent an invalid readiness status.',
      );
    }
    final rawComponents = _requiredObject(json, 'components');
    final components = <String, String>{};
    for (final entry in rawComponents.entries) {
      if (!_readinessComponents.contains(entry.key) ||
          entry.value is! String ||
          !_componentStatuses.contains(entry.value)) {
        throw const RealtimeProtocolException(
          'invalid_readiness',
          'The realtime service sent invalid component readiness.',
        );
      }
      components[entry.key] = entry.value! as String;
    }
    if (!components.keys.toSet().containsAll(_readinessComponents)) {
      throw const RealtimeProtocolException(
        'invalid_readiness',
        'The realtime service omitted component readiness.',
      );
    }
    if (status == 'ready' &&
        components.values.any((value) => value != 'ready')) {
      throw const RealtimeProtocolException(
        'invalid_readiness',
        'The realtime service sent inconsistent component readiness.',
      );
    }
    return RealtimeReadiness(
      status: status,
      components: Map<String, String>.unmodifiable(components),
      message: _requiredString(json, 'message', maxLength: 160),
    );
  }

  final String status;
  final Map<String, String> components;
  final String message;

  bool get isReady => status == 'ready';
}

final class RealtimeSessionReadyEvent extends RealtimeServerEvent {
  const RealtimeSessionReadyEvent({
    required super.protocolVersion,
    required this.sessionId,
    required this.companion,
    required this.aiDisclosure,
    required this.audioFormat,
    required this.readiness,
    required this.canProcessTurns,
  });

  final String sessionId;
  final String companion;
  final String aiDisclosure;
  final RealtimeAudioFormat audioFormat;
  final RealtimeReadiness readiness;
  final bool canProcessTurns;
}

sealed class RealtimeTurnEvent extends RealtimeServerEvent {
  const RealtimeTurnEvent({
    required super.protocolVersion,
    required this.turnId,
  });

  final String turnId;
}

final class RealtimeSttPartialEvent extends RealtimeTurnEvent {
  const RealtimeSttPartialEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.text,
  });

  final String text;
}

final class RealtimeSttFinalEvent extends RealtimeTurnEvent {
  const RealtimeSttFinalEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.text,
  });

  final String text;
}

final class RealtimeThinkingEvent extends RealtimeTurnEvent {
  const RealtimeThinkingEvent({
    required super.protocolVersion,
    required super.turnId,
  });
}

/// One crisis resource the server asks the client to show on screen.
final class RealtimeCrisisResource {
  const RealtimeCrisisResource({required this.label, required this.phone});

  final String label;
  final String phone;
}

/// The server answered this turn with crisis resources instead of the model.
final class RealtimeSafetyEscalationEvent extends RealtimeTurnEvent {
  RealtimeSafetyEscalationEvent({
    required super.protocolVersion,
    required super.turnId,
    required List<RealtimeCrisisResource> resources,
  }) : resources = List<RealtimeCrisisResource>.unmodifiable(resources);

  static const String crisisResourcesKind = 'crisis_resources';

  final List<RealtimeCrisisResource> resources;
}

final class RealtimeTextDeltaEvent extends RealtimeTurnEvent {
  const RealtimeTextDeltaEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.delta,
  });

  final String delta;
}

final class RealtimeTextSentenceEvent extends RealtimeTurnEvent {
  const RealtimeTextSentenceEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.sequence,
    required this.text,
  });

  final int sequence;
  final String text;
}

final class RealtimeAudioChunkEvent extends RealtimeTurnEvent {
  const RealtimeAudioChunkEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.sequence,
    required this.byteLength,
    required this.audioFormat,
    this.audioDurationMs,
    this.synthesisMs,
    this.realtimeFactor,
  });

  final int sequence;
  final int byteLength;
  final RealtimeAudioFormat audioFormat;
  final double? audioDurationMs;
  final double? synthesisMs;
  final double? realtimeFactor;
}

final class RealtimeSpeakingEvent extends RealtimeTurnEvent {
  const RealtimeSpeakingEvent({
    required super.protocolVersion,
    required super.turnId,
  });
}

final class RealtimeTurnCompleteEvent extends RealtimeTurnEvent {
  const RealtimeTurnCompleteEvent({
    required super.protocolVersion,
    required super.turnId,
    required this.metrics,
    required this.cancelled,
  });

  final Map<String, double> metrics;
  final bool cancelled;
}

final class RealtimeErrorEvent extends RealtimeServerEvent {
  const RealtimeErrorEvent({
    required super.protocolVersion,
    required this.code,
    required this.message,
    required this.recoverable,
    this.turnId,
  });

  final String code;
  final String message;
  final bool recoverable;
  final String? turnId;
}

/// Decodes bounded server JSON into typed events. Binary audio is deliberately
/// handled by the WebSocket client only after an [RealtimeAudioChunkEvent].
abstract final class RealtimeServerEventParser {
  static RealtimeServerEvent parse(String wireText) {
    if (utf8.encode(wireText).length >
        AiraRealtimeProtocol.maxServerControlBytes) {
      throw const RealtimeProtocolException(
        'message_too_large',
        'The realtime service sent an oversized control message.',
      );
    }

    final Object? decoded;
    try {
      decoded = jsonDecode(wireText);
    } on FormatException {
      throw const RealtimeProtocolException(
        'malformed_json',
        'The realtime service sent malformed JSON.',
      );
    }
    final json = _asObject(decoded, 'message');
    final type = _requiredString(json, 'type', maxLength: 48);
    final protocolVersion = _requiredInt(json, 'protocol_version');
    if (protocolVersion != AiraRealtimeProtocol.version) {
      throw const RealtimeProtocolException(
        'unsupported_protocol_version',
        'The realtime service uses an unsupported protocol version.',
      );
    }

    return switch (type) {
      'session_ready' => _sessionReady(json, protocolVersion),
      'stt_partial' => RealtimeSttPartialEvent(
        protocolVersion: protocolVersion,
        turnId: _turnId(json),
        text: _requiredString(
          json,
          'text',
          maxLength: AiraRealtimeProtocol.maxTextCharacters,
        ),
      ),
      'stt_final' => RealtimeSttFinalEvent(
        protocolVersion: protocolVersion,
        turnId: _turnId(json),
        text: _requiredString(
          json,
          'text',
          maxLength: AiraRealtimeProtocol.maxTextCharacters,
        ),
      ),
      'thinking' => RealtimeThinkingEvent(
        protocolVersion: protocolVersion,
        turnId: _turnId(json),
      ),
      'safety_escalation' => _safetyEscalation(json, protocolVersion),
      'text_delta' => RealtimeTextDeltaEvent(
        protocolVersion: protocolVersion,
        turnId: _turnId(json),
        delta: _requiredString(
          json,
          'delta',
          maxLength: AiraRealtimeProtocol.maxTextDeltaCharacters,
        ),
      ),
      'text_sentence' => _textSentence(json, protocolVersion),
      'audio_chunk' => _audioChunk(json, protocolVersion),
      'speaking' => RealtimeSpeakingEvent(
        protocolVersion: protocolVersion,
        turnId: _turnId(json),
      ),
      'turn_complete' => _turnComplete(json, protocolVersion),
      'recoverable_error' => _error(json, protocolVersion, true),
      'fatal_error' => _error(json, protocolVersion, false),
      'error' => _error(
        json,
        protocolVersion,
        _optionalBool(json, 'recoverable') ?? true,
      ),
      _ => throw RealtimeProtocolException(
        'unknown_event',
        'The realtime service sent an unsupported event: $type.',
      ),
    };
  }

  static RealtimeSessionReadyEvent _sessionReady(
    Map<String, Object?> json,
    int protocolVersion,
  ) {
    final sessionId = _safeId(json, 'session_id');
    final companion = _requiredString(json, 'companion', maxLength: 32);
    if (companion != AiraRealtimeProtocol.companionId) {
      throw const RealtimeProtocolException(
        'unsupported_companion',
        'The realtime session selected an unsupported companion.',
      );
    }
    final disclosure = _requiredString(json, 'ai_disclosure', maxLength: 500);
    if (!RegExp(r'\bAI\b', caseSensitive: false).hasMatch(disclosure)) {
      throw const RealtimeProtocolException(
        'missing_ai_disclosure',
        'The realtime session omitted its AI disclosure.',
      );
    }
    final readiness = RealtimeReadiness.fromJson(
      _requiredObject(json, 'readiness'),
    );
    final canProcessTurns = _requiredBool(json, 'can_process_turns');
    if (canProcessTurns && !readiness.isReady) {
      throw const RealtimeProtocolException(
        'invalid_readiness',
        'The realtime service advertised inconsistent readiness.',
      );
    }
    return RealtimeSessionReadyEvent(
      protocolVersion: protocolVersion,
      sessionId: sessionId,
      companion: companion,
      aiDisclosure: disclosure,
      audioFormat: RealtimeAudioFormat.fromJson(
        _requiredObject(json, 'audio_format'),
      ),
      readiness: readiness,
      canProcessTurns: canProcessTurns,
    );
  }

  static RealtimeAudioChunkEvent _audioChunk(
    Map<String, Object?> json,
    int protocolVersion,
  ) {
    final byteLength = _requiredInt(json, 'byte_length');
    if (byteLength <= 0 ||
        byteLength > AiraRealtimeProtocol.maxFrameBytes ||
        byteLength.isOdd) {
      throw const RealtimeProtocolException(
        'invalid_audio_chunk',
        'The realtime service announced an invalid audio chunk.',
      );
    }
    final sequence = _requiredInt(json, 'sequence');
    if (sequence < 0) {
      throw const RealtimeProtocolException(
        'invalid_audio_chunk',
        'The realtime service announced an invalid audio sequence.',
      );
    }
    final audioFormat = RealtimeAudioFormat.fromPlaybackJson(<String, Object?>{
      'encoding': json['encoding'],
      'sample_rate_hz': json['sample_rate_hz'],
      'channels': json['channels'],
    });
    if (byteLength % (audioFormat.channels * 2) != 0) {
      throw const RealtimeProtocolException(
        'invalid_audio_chunk',
        'The realtime service announced an incomplete PCM audio frame.',
      );
    }
    return RealtimeAudioChunkEvent(
      protocolVersion: protocolVersion,
      turnId: _turnId(json),
      sequence: sequence,
      byteLength: byteLength,
      audioFormat: audioFormat,
      audioDurationMs: _optionalFiniteNumber(
        json,
        'audio_duration_ms',
        positive: true,
      ),
      synthesisMs: _optionalFiniteNumber(json, 'synthesis_ms'),
      realtimeFactor: _optionalFiniteNumber(json, 'realtime_factor'),
    );
  }

  static RealtimeSafetyEscalationEvent _safetyEscalation(
    Map<String, Object?> json,
    int protocolVersion,
  ) {
    const invalid = RealtimeProtocolException(
      'invalid_safety_escalation',
      'The realtime service sent an invalid safety escalation.',
    );
    final kind = _requiredString(json, 'kind', maxLength: 32);
    if (kind != RealtimeSafetyEscalationEvent.crisisResourcesKind) {
      throw invalid;
    }
    final rawResources = json['resources'];
    if (rawResources is! List<Object?> ||
        rawResources.isEmpty ||
        rawResources.length > 4) {
      throw invalid;
    }
    final phonePattern = RegExp(r'^\+?[0-9][0-9 -]{1,18}$');
    final resources = <RealtimeCrisisResource>[];
    for (final rawResource in rawResources) {
      final resource = _asObject(rawResource, 'resource');
      final phone = _requiredString(resource, 'phone', maxLength: 20);
      if (!phonePattern.hasMatch(phone)) throw invalid;
      resources.add(
        RealtimeCrisisResource(
          label: _requiredString(resource, 'label', maxLength: 80),
          phone: phone,
        ),
      );
    }
    return RealtimeSafetyEscalationEvent(
      protocolVersion: protocolVersion,
      turnId: _turnId(json),
      resources: resources,
    );
  }

  static RealtimeTextSentenceEvent _textSentence(
    Map<String, Object?> json,
    int protocolVersion,
  ) {
    final sequence = _requiredInt(json, 'sequence');
    if (sequence <= 0) {
      throw const RealtimeProtocolException(
        'invalid_sequence',
        'The realtime service sent an invalid sentence sequence.',
      );
    }
    return RealtimeTextSentenceEvent(
      protocolVersion: protocolVersion,
      turnId: _turnId(json),
      sequence: sequence,
      text: _requiredString(
        json,
        'text',
        maxLength: AiraRealtimeProtocol.maxTextCharacters,
      ),
    );
  }

  static RealtimeTurnCompleteEvent _turnComplete(
    Map<String, Object?> json,
    int protocolVersion,
  ) {
    final rawMetrics = json['metrics'];
    final metrics = <String, double>{};
    if (rawMetrics != null) {
      final metricObject = _asObject(rawMetrics, 'metrics');
      for (final entry in metricObject.entries) {
        final value = entry.value;
        if (value is! num || !value.isFinite || value < 0) {
          throw const RealtimeProtocolException(
            'invalid_metrics',
            'The realtime service sent invalid turn metrics.',
          );
        }
        metrics[entry.key] = value.toDouble();
      }
    }
    return RealtimeTurnCompleteEvent(
      protocolVersion: protocolVersion,
      turnId: _turnId(json),
      metrics: Map<String, double>.unmodifiable(metrics),
      cancelled: _optionalBool(json, 'cancelled') ?? false,
    );
  }

  static RealtimeErrorEvent _error(
    Map<String, Object?> json,
    int protocolVersion,
    bool recoverable,
  ) {
    final rawTurnId = json['turn_id'];
    final turnId = rawTurnId == null ? null : _safeId(json, 'turn_id');
    return RealtimeErrorEvent(
      protocolVersion: protocolVersion,
      code: _requiredString(json, 'code', maxLength: 80),
      message: _requiredString(json, 'message', maxLength: 500),
      recoverable: recoverable,
      turnId: turnId,
    );
  }
}

Map<String, Object?> _asObject(Object? value, String field) {
  if (value is! Map) {
    throw RealtimeProtocolException(
      'invalid_$field',
      'The realtime service sent an invalid $field.',
    );
  }
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    if (entry.key is! String) {
      throw RealtimeProtocolException(
        'invalid_$field',
        'The realtime service sent an invalid $field.',
      );
    }
    result[entry.key as String] = entry.value;
  }
  return result;
}

Map<String, Object?> _requiredObject(Map<String, Object?> json, String field) {
  return _asObject(json[field], field);
}

String _requiredString(
  Map<String, Object?> json,
  String field, {
  required int maxLength,
}) {
  final value = json[field];
  if (value is! String ||
      value.trim().isEmpty ||
      value.runes.length > maxLength) {
    throw RealtimeProtocolException(
      'invalid_$field',
      'The realtime service sent an invalid $field.',
    );
  }
  return value;
}

int _requiredInt(Map<String, Object?> json, String field) {
  final value = json[field];
  if (value is! int) {
    throw RealtimeProtocolException(
      'invalid_$field',
      'The realtime service sent an invalid $field.',
    );
  }
  return value;
}

double? _optionalFiniteNumber(
  Map<String, Object?> json,
  String field, {
  bool positive = false,
}) {
  final value = json[field];
  if (value == null) return null;
  if (value is! num ||
      !value.isFinite ||
      value < 0 ||
      (positive && value <= 0)) {
    throw RealtimeProtocolException(
      'invalid_$field',
      'The realtime service sent an invalid $field.',
    );
  }
  return value.toDouble();
}

bool? _optionalBool(Map<String, Object?> json, String field) {
  final value = json[field];
  if (value == null || value is bool) return value as bool?;
  throw RealtimeProtocolException(
    'invalid_$field',
    'The realtime service sent an invalid $field.',
  );
}

bool _requiredBool(Map<String, Object?> json, String field) {
  final value = json[field];
  if (value is bool) return value;
  throw RealtimeProtocolException(
    'invalid_$field',
    'The realtime service sent an invalid $field.',
  );
}

String _turnId(Map<String, Object?> json) => _safeId(json, 'turn_id');

String _safeId(Map<String, Object?> json, String field) {
  final value = _requiredString(json, field, maxLength: 120);
  if (!_safeIdPattern.hasMatch(value)) {
    throw RealtimeProtocolException(
      'invalid_$field',
      'The realtime service sent an invalid $field.',
    );
  }
  return value;
}

final RegExp _safeIdPattern = RegExp(r'^[a-z0-9][a-z0-9_-]{0,119}$');

const Set<String> _readinessStatuses = <String>{
  'starting',
  'warming',
  'ready',
  'degraded',
  'failed',
};
const Set<String> _readinessComponents = <String>{'stt', 'llm', 'tts'};
const Set<String> _componentStatuses = <String>{
  'not_loaded',
  'loading',
  'ready',
  'degraded',
  'failed',
};
