import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../../../core/config/aira_api_config.dart';
import '../domain/conversation_turn_response.dart';
import 'conversation_api.dart';

final class HttpConversationApi implements ConversationApi {
  HttpConversationApi({
    required this.config,
    required this.client,
    this.healthTimeout = const Duration(seconds: 4),
    this.conversationTimeout = const Duration(minutes: 5),
  }) {
    if (healthTimeout <= Duration.zero ||
        conversationTimeout <= Duration.zero) {
      throw ArgumentError('HTTP timeouts must be positive.');
    }
  }

  static const int _maximumJsonBytes = 64 * 1024;

  final AiraApiConfig config;
  final http.Client client;
  final Duration healthTimeout;
  final Duration conversationTimeout;
  final Set<Completer<void>> _activeAborters = <Completer<void>>{};

  bool _disposed = false;
  bool _turnInFlight = false;

  @override
  Future<bool> checkHealth() async {
    if (_disposed) return false;

    final aborter = _newAborter();
    final request = http.AbortableRequest(
      'GET',
      config.healthUri,
      abortTrigger: aborter.future,
    );
    try {
      final response = await _sendAndRead(request).timeout(
        healthTimeout,
        onTimeout: () {
          _abort(aborter);
          throw TimeoutException('Local health check timed out.');
        },
      );
      _debugLog('health status=${response.statusCode}');
      if (response.statusCode != 200) return false;

      final decoded = jsonDecode(response.body);
      return decoded is Map<String, Object?> &&
          decoded['status'] == 'ok' &&
          decoded['service'] == 'aira-local-voice-api';
    } catch (_) {
      _debugLog('health result=offline');
      return false;
    } finally {
      _activeAborters.remove(aborter);
    }
  }

  @override
  Future<ConversationTurnResponse> createAanyaTurn(String audioPath) async {
    if (_disposed) {
      throw const ConversationApiException(
        ConversationApiErrorKind.unavailable,
      );
    }
    if (_turnInFlight) {
      throw const ConversationApiException(
        ConversationApiErrorKind.serviceBusy,
      );
    }
    _turnInFlight = true;

    final aborter = _newAborter();
    try {
      final file = File(audioPath);
      if (audioPath.trim().isEmpty ||
          !audioPath.toLowerCase().endsWith('.wav') ||
          !await file.exists() ||
          await file.length() == 0) {
        throw const ConversationApiException(
          ConversationApiErrorKind.invalidRequest,
        );
      }

      final upload = await http.MultipartFile.fromPath(
        'audio',
        audioPath,
        filename: 'recording.wav',
        contentType: http.MediaType('audio', 'wav'),
      );
      final request =
          http.AbortableMultipartRequest(
              'POST',
        config.conversationTurnUri,
              abortTrigger: aborter.future,
            )
            ..fields['companion'] = 'aanya'
            ..files.add(upload);

      final stopwatch = Stopwatch()..start();
      _debugLog('conversation request=start');
      final response = await _sendAndRead(request).timeout(
        conversationTimeout,
        onTimeout: () {
          _abort(aborter);
          throw TimeoutException('Local conversation request timed out.');
        },
      );
      stopwatch.stop();
      _debugLog(
        'conversation request=end status=${response.statusCode} '
        'roundtrip_ms=${stopwatch.elapsedMilliseconds}',
      );

      _throwForStatus(response.statusCode);
      final decoded = jsonDecode(response.body);
      if (decoded is! Map<String, Object?>) {
        throw const FormatException('Malformed conversation response.');
      }

      final result = ConversationTurnResponse.fromJson(
        decoded,
        resolveAudioUri: config.resolveTurnAudioUrl,
      );
      if (result.companion != 'aanya') {
        throw const FormatException('Unexpected companion response.');
      }
      _debugLog('conversation turn_id=${result.turnId}');
      return result;
    } on ConversationApiException {
      rethrow;
    } on TimeoutException {
      _debugLog('conversation error=timeout');
      throw const ConversationApiException(ConversationApiErrorKind.timeout);
    } on http.ClientException {
      _debugLog('conversation error=unavailable');
      throw const ConversationApiException(
        ConversationApiErrorKind.unavailable,
      );
    } on SocketException {
      _debugLog('conversation error=unavailable');
      throw const ConversationApiException(
        ConversationApiErrorKind.unavailable,
      );
    } on FileSystemException {
      _debugLog('conversation error=invalid_request');
      throw const ConversationApiException(
        ConversationApiErrorKind.invalidRequest,
      );
    } on FormatException {
      _debugLog('conversation error=unexpected_response');
      throw const ConversationApiException(
        ConversationApiErrorKind.unexpectedResponse,
      );
    } catch (_) {
      _debugLog('conversation error=processing_failure');
      throw const ConversationApiException(
        ConversationApiErrorKind.processingFailure,
      );
    } finally {
      _activeAborters.remove(aborter);
      _turnInFlight = false;
    }
  }

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    for (final aborter in _activeAborters.toList(growable: false)) {
      _abort(aborter);
    }
    _activeAborters.clear();
    client.close();
  }

  Completer<void> _newAborter() {
    final aborter = Completer<void>();
    _activeAborters.add(aborter);
    return aborter;
  }

  static void _abort(Completer<void> aborter) {
    if (!aborter.isCompleted) aborter.complete();
  }

  static void _throwForStatus(int statusCode) {
    if (statusCode >= 200 && statusCode < 300) return;
    if (statusCode == 400 || statusCode == 413 || statusCode == 422) {
      throw const ConversationApiException(
        ConversationApiErrorKind.invalidRequest,
      );
    }
    if (statusCode == 503) {
      throw const ConversationApiException(
        ConversationApiErrorKind.serviceBusy,
      );
    }
    if (statusCode >= 500) {
      throw const ConversationApiException(
        ConversationApiErrorKind.processingFailure,
      );
    }
    throw const ConversationApiException(
      ConversationApiErrorKind.unexpectedResponse,
    );
  }

  Future<_BufferedResponse> _sendAndRead(http.BaseRequest request) async {
    final response = await client.send(request);
    final bytes = BytesBuilder(copy: false);
    await for (final chunk in response.stream) {
      if (bytes.length + chunk.length > _maximumJsonBytes) {
        throw const FormatException('Response is too large.');
      }
      bytes.add(chunk);
    }
    return _BufferedResponse(
      statusCode: response.statusCode,
      body: utf8.decode(bytes.takeBytes()),
    );
  }

  static void _debugLog(String message) {
    if (kDebugMode) debugPrint('[Aira local API] $message');
  }
}

final class _BufferedResponse {
  const _BufferedResponse({required this.statusCode, required this.body});

  final int statusCode;
  final String body;
}
