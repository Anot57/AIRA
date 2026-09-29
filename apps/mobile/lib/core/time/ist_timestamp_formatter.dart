/// Formats message creation instants in the MVP's fixed Asia/Kolkata timezone.
///
/// India Standard Time does not observe daylight-saving time, so converting
/// from UTC with its fixed offset is deterministic on every Android device.
abstract final class IstTimestampFormatter {
  static const Duration _utcOffset = Duration(hours: 5, minutes: 30);

  static DateTime toIst(DateTime instant) => instant.toUtc().add(_utcOffset);

  static String format(DateTime instant) {
    final ist = toIst(instant);
    final hours = ist.hour.toString().padLeft(2, '0');
    final minutes = ist.minute.toString().padLeft(2, '0');
    final seconds = ist.second.toString().padLeft(2, '0');
    return '$hours:$minutes:$seconds';
  }
}
