import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

abstract interface class RealtimeWebSocket {
  Stream<Object?> get messages;

  void sendText(String text);

  void sendBinary(Uint8List bytes);

  Future<void> close([int? code, String? reason]);
}

abstract interface class RealtimeWebSocketConnector {
  Future<RealtimeWebSocket> connect(Uri uri);
}

/// Android implementation backed only by dart:io; no extra transport package
/// is required. The server is expected to be a trusted local-development host.
final class IoRealtimeWebSocketConnector implements RealtimeWebSocketConnector {
  const IoRealtimeWebSocketConnector({
    this.connectionTimeout = const Duration(seconds: 8),
    this.pingInterval = const Duration(seconds: 15),
  });

  final Duration connectionTimeout;
  final Duration pingInterval;

  @override
  Future<RealtimeWebSocket> connect(Uri uri) async {
    if ((uri.scheme != 'ws' && uri.scheme != 'wss') || uri.host.isEmpty) {
      throw ArgumentError.value(uri, 'uri', 'Must be a ws or wss endpoint.');
    }
    final socket = await WebSocket.connect(
      uri.toString(),
      compression: CompressionOptions.compressionOff,
    ).timeout(connectionTimeout);
    socket.pingInterval = pingInterval;
    return _IoRealtimeWebSocket(socket);
  }
}

final class _IoRealtimeWebSocket implements RealtimeWebSocket {
  const _IoRealtimeWebSocket(this._socket);

  final WebSocket _socket;

  @override
  Stream<Object?> get messages => _socket;

  @override
  void sendText(String text) => _socket.add(text);

  @override
  void sendBinary(Uint8List bytes) => _socket.add(bytes);

  @override
  Future<void> close([int? code, String? reason]) async {
    await _socket.close(code, reason);
  }
}
