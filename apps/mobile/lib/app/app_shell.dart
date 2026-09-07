import 'package:flutter/material.dart';

import '../features/call/aanya_voice_call_screen.dart';
import '../features/call/mock_voice_call_screen.dart';
import '../features/companions/data/companion_catalog.dart';
import '../features/companions/data/local_companion_catalog.dart';
import '../features/companions/domain/companion.dart';
import '../features/companions/presentation/companion_details_screen.dart';
import '../features/companions/presentation/companion_grid_screen.dart';
import '../features/schedule/schedule_screen.dart';
import '../features/settings/settings_screen.dart';

class AppShell extends StatefulWidget {
  const AppShell({
    super.key,
    required this.themeMode,
    required this.onThemeModeChanged,
    this.companionCatalog,
    this.aanyaVoiceCallBuilder,
  });

  final ThemeMode themeMode;
  final ValueChanged<ThemeMode> onThemeModeChanged;
  final CompanionCatalog? companionCatalog;
  final AanyaVoiceCallBuilder? aanyaVoiceCallBuilder;

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  final _localCompanionCatalog = LocalCompanionCatalog();
  var _currentIndex = 0;
  var _memoryEnabled = false;
  var _selectedTime = const TimeOfDay(hour: 19, minute: 30);
  var _selectedDays = <int>{
    DateTime.monday,
    DateTime.wednesday,
    DateTime.friday,
  };

  void _openCompanion(Companion companion) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (detailsContext) => CompanionDetailsScreen(
          companion: companion,
          onStartVoiceCall: () {
            Navigator.of(detailsContext).push(
              MaterialPageRoute<void>(
                builder: (callContext) {
                  void onEnd() => Navigator.of(callContext).pop();
                  if (companion.id == 'aanya' &&
                      widget.aanyaVoiceCallBuilder != null) {
                    return widget.aanyaVoiceCallBuilder!(
                      companion: companion,
                      onEnd: onEnd,
                    );
                  }
                  return MockVoiceCallScreen(
                    companion: companion,
                    onEnd: onEnd,
                  );
                },
              ),
            );
          },
        ),
      ),
    );
  }

  void _showScheduleSaved() {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        const SnackBar(content: Text('Conversation schedule saved locally.')),
      );
  }

  @override
  Widget build(BuildContext context) {
    final screens = <Widget>[
      CompanionGridScreen(
        catalog: widget.companionCatalog ?? _localCompanionCatalog,
        onCompanionSelected: _openCompanion,
      ),
      ScheduleScreen(
        selectedTime: _selectedTime,
        selectedDays: _selectedDays,
        onTimeChanged: (time) => setState(() => _selectedTime = time),
        onDaysChanged: (days) => setState(() => _selectedDays = days),
        onSaved: _showScheduleSaved,
      ),
      SettingsScreen(
        memoryEnabled: _memoryEnabled,
        onMemoryChanged: (enabled) {
          setState(() => _memoryEnabled = enabled);
        },
        themeMode: widget.themeMode,
        onThemeModeChanged: widget.onThemeModeChanged,
      ),
    ];

    return Scaffold(
      body: IndexedStack(index: _currentIndex, children: screens),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (index) {
          setState(() => _currentIndex = index);
        },
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.people_alt_outlined),
            selectedIcon: Icon(Icons.people_alt_rounded),
            label: 'Companions',
          ),
          NavigationDestination(
            icon: Icon(Icons.calendar_today_outlined),
            selectedIcon: Icon(Icons.calendar_today_rounded),
            label: 'Schedule',
          ),
          NavigationDestination(
            icon: Icon(Icons.tune_outlined),
            selectedIcon: Icon(Icons.tune_rounded),
            label: 'Settings',
          ),
        ],
      ),
    );
  }
}
