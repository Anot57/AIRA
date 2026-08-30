import 'package:flutter/material.dart';

import '../../core/constants/app_copy.dart';

/// A controlled editor for a repeating local conversation schedule.
class ScheduleScreen extends StatelessWidget {
  const ScheduleScreen({
    super.key,
    required this.selectedTime,
    required this.selectedDays,
    required this.onTimeChanged,
    required this.onDaysChanged,
    required this.onSaved,
  });

  final TimeOfDay selectedTime;
  final Set<int> selectedDays;
  final ValueChanged<TimeOfDay> onTimeChanged;
  final ValueChanged<Set<int>> onDaysChanged;
  final VoidCallback onSaved;

  Future<void> _selectTime(BuildContext context) async {
    final result = await showTimePicker(
      context: context,
      initialTime: selectedTime,
      helpText: 'Choose conversation time',
      confirmText: 'Use time',
    );

    if (result != null && context.mounted) {
      onTimeChanged(result);
    }
  }

  void _toggleDay(int day, bool selected) {
    final updatedDays = Set<int>.of(selectedDays);
    if (selected) {
      updatedDays.add(day);
    } else {
      updatedDays.remove(day);
    }
    onDaysChanged(Set<int>.unmodifiable(updatedDays));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;
    final formattedTime = MaterialLocalizations.of(context).formatTimeOfDay(
      selectedTime,
      alwaysUse24HourFormat: MediaQuery.alwaysUse24HourFormatOf(context),
    );

    return SafeArea(
      child: LayoutBuilder(
        builder: (context, constraints) {
          final horizontalPadding = constraints.maxWidth < 400 ? 20.0 : 28.0;

          return SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(
              horizontalPadding,
              24,
              horizontalPadding,
              32,
            ),
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 640),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Semantics(
                      header: true,
                      child: Text(
                        'Schedule',
                        style: theme.textTheme.headlineMedium?.copyWith(
                          fontWeight: FontWeight.w700,
                          letterSpacing: -0.5,
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Choose when ${AppCopy.productName} should prepare a '
                      'local AI conversation reminder.',
                      style: theme.textTheme.bodyLarge?.copyWith(
                        color: colorScheme.onSurfaceVariant,
                        height: 1.45,
                      ),
                    ),
                    const SizedBox(height: 28),
                    _SectionCard(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Text(
                            'TIME',
                            style: theme.textTheme.labelMedium?.copyWith(
                              color: colorScheme.primary,
                              fontWeight: FontWeight.w800,
                              letterSpacing: 1.2,
                            ),
                          ),
                          const SizedBox(height: 12),
                          Semantics(
                            button: true,
                            label:
                                'Change scheduled time. Current time: '
                                '$formattedTime',
                            onTap: () => _selectTime(context),
                            child: ExcludeSemantics(
                              child: OutlinedButton(
                                onPressed: () => _selectTime(context),
                                style: OutlinedButton.styleFrom(
                                  minimumSize: const Size.fromHeight(72),
                                  alignment: Alignment.centerLeft,
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 18,
                                  ),
                                ),
                                child: Row(
                                  children: [
                                    Icon(
                                      Icons.access_time_rounded,
                                      color: colorScheme.primary,
                                    ),
                                    const SizedBox(width: 14),
                                    Expanded(
                                      child: Text(
                                        formattedTime,
                                        style: theme.textTheme.headlineSmall
                                            ?.copyWith(
                                              fontWeight: FontWeight.w700,
                                            ),
                                      ),
                                    ),
                                    const Icon(Icons.edit_rounded, size: 20),
                                  ],
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 16),
                    _SectionCard(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'REPEAT',
                            style: theme.textTheme.labelMedium?.copyWith(
                              color: colorScheme.primary,
                              fontWeight: FontWeight.w800,
                              letterSpacing: 1.2,
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            'Select one or more days',
                            style: theme.textTheme.bodyMedium?.copyWith(
                              color: colorScheme.onSurfaceVariant,
                            ),
                          ),
                          const SizedBox(height: 16),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: _days.map((day) {
                              final isSelected = selectedDays.contains(day.id);
                              return Semantics(
                                label: day.name,
                                button: true,
                                selected: isSelected,
                                onTap: () => _toggleDay(day.id, !isSelected),
                                child: ExcludeSemantics(
                                  child: FilterChip(
                                    label: Text(day.shortName),
                                    selected: isSelected,
                                    showCheckmark: true,
                                    onSelected: (value) =>
                                        _toggleDay(day.id, value),
                                  ),
                                ),
                              );
                            }).toList(),
                          ),
                          if (selectedDays.isEmpty) ...[
                            const SizedBox(height: 12),
                            Text(
                              'Choose at least one day to save.',
                              style: theme.textTheme.bodySmall?.copyWith(
                                color: colorScheme.error,
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                    const SizedBox(height: 16),
                    Container(
                      padding: const EdgeInsets.all(18),
                      decoration: BoxDecoration(
                        color: colorScheme.secondaryContainer.withValues(
                          alpha: 0.55,
                        ),
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            Icons.notifications_none_rounded,
                            color: colorScheme.onSecondaryContainer,
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              'This milestone only previews a schedule on '
                              'this device. It will not place a real call or '
                              'send a notification.',
                              style: theme.textTheme.bodyMedium?.copyWith(
                                color: colorScheme.onSecondaryContainer,
                                height: 1.4,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 28),
                    FilledButton.icon(
                      onPressed: selectedDays.isEmpty ? null : onSaved,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size.fromHeight(54),
                      ),
                      icon: const Icon(Icons.check_rounded),
                      label: const Text('Save schedule'),
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final colorScheme = Theme.of(context).colorScheme;

    return Card(
      margin: EdgeInsets.zero,
      elevation: 0,
      color: colorScheme.surfaceContainerLow,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
      child: Padding(padding: const EdgeInsets.all(20), child: child),
    );
  }
}

class _DayOption {
  const _DayOption(this.id, this.shortName, this.name);

  final int id;
  final String shortName;
  final String name;
}

const _days = <_DayOption>[
  _DayOption(DateTime.monday, 'Mon', 'Monday'),
  _DayOption(DateTime.tuesday, 'Tue', 'Tuesday'),
  _DayOption(DateTime.wednesday, 'Wed', 'Wednesday'),
  _DayOption(DateTime.thursday, 'Thu', 'Thursday'),
  _DayOption(DateTime.friday, 'Fri', 'Friday'),
  _DayOption(DateTime.saturday, 'Sat', 'Saturday'),
  _DayOption(DateTime.sunday, 'Sun', 'Sunday'),
];
