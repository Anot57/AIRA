import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

import '../application/voice_io.dart';

/// Allocates non-persistent, opaque WAV paths in the application cache.
final class AppTemporaryRecordingStore implements TemporaryRecordingStore {
  AppTemporaryRecordingStore({
    Future<Directory> Function()? temporaryDirectory,
    Random? random,
  }) : _temporaryDirectory = temporaryDirectory ?? getTemporaryDirectory,
       _random = random ?? Random.secure();

  final Future<Directory> Function() _temporaryDirectory;
  final Random _random;
  final Set<String> _issuedPaths = <String>{};
  var _sequence = 0;

  @override
  Future<String> allocateWavPath() async {
    final root = await _temporaryDirectory();
    final voiceDirectory = Directory(
      '${root.path}${Platform.pathSeparator}aira_voice',
    );
    await voiceDirectory.create(recursive: true);

    for (var attempt = 0; attempt < 8; attempt += 1) {
      _sequence += 1;
      final timestamp = DateTime.now().microsecondsSinceEpoch;
      final nonce = _random.nextInt(0x7fffffff).toRadixString(16);
      final filename = 'aanya_${timestamp}_${_sequence}_$nonce.wav';
      final path = File(
        '${voiceDirectory.path}${Platform.pathSeparator}$filename',
      ).absolute.path;
      if (!_issuedPaths.contains(path) && !await File(path).exists()) {
        _issuedPaths.add(path);
        return path;
      }
    }
    throw const FileSystemException(
      'Could not allocate a unique temporary recording.',
    );
  }

  @override
  Future<void> delete(String path) async {
    final normalizedPath = File(path).absolute.path;
    if (!_issuedPaths.contains(normalizedPath)) return;

    try {
      final file = File(normalizedPath);
      if (await file.exists()) await file.delete();
      _issuedPaths.remove(normalizedPath);
    } on FileSystemException {
      if (kDebugMode) {
        debugPrint('Aanya voice: temporary recording cleanup failed.');
      }
    }
  }

  @override
  Future<void> dispose() async {
    final paths = List<String>.of(_issuedPaths);
    for (final path in paths) {
      await delete(path);
    }
  }
}
