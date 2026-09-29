import 'package:female_voice_ai/core/time/ist_timestamp_formatter.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('IstTimestampFormatter', () {
    test('formats midnight, morning, and afternoon with leading zeros', () {
      expect(
        IstTimestampFormatter.format(DateTime.utc(2026, 1, 1, 18, 30)),
        '00:00:00',
      );
      expect(
        IstTimestampFormatter.format(DateTime.utc(2026, 1, 1, 3, 33, 7)),
        '09:03:07',
      );
      expect(
        IstTimestampFormatter.format(DateTime.utc(2026, 1, 1, 13, 15, 59)),
        '18:45:59',
      );
    });

    test('converts UTC to IST across a day rollover', () {
      final instant = DateTime.utc(2026, 1, 31, 18, 29, 1);
      final converted = IstTimestampFormatter.toIst(instant);

      expect(converted, DateTime.utc(2026, 1, 31, 23, 59, 1));
      expect(IstTimestampFormatter.format(instant), '23:59:01');

      final rollover = IstTimestampFormatter.toIst(
        DateTime.utc(2026, 1, 31, 18, 31),
      );
      expect(rollover.day, 1);
      expect(rollover.month, 2);
    });
  });
}
