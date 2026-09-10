import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

abstract interface class RealtimeWebSocket {
  Stream<Object?> get messages;

  void sendText(String text);

  void sendBinary(Uint8List bytes);

  /// Completes once the frame has been accepted by the ordered socket sink.
  Future<void> sendBinaryComplete(Uint8List bytes) async => sendBinary(bytes);

  /// Completes once the control frame has been accepted by the ordered sink.
  Future<void> sendTextComplete(String text) async => sendText(text);

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
  Future<void> sendBinaryComplete(Uint8List bytes) =>
      _socket.addStream(Stream<Object>.value(bytes));

  @override
  Future<void> sendTextComplete(String text) =>
      _socket.addStream(Stream<Object>.value(text));

  @override
  Future<void> close([int? code, String? reason]) async {
    await _socket.close(code, reason);
  }
}
