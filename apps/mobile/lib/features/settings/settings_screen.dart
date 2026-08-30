import 'package:flutter/material.dart';

import '../../core/constants/app_copy.dart';

/// Privacy, memory consent, and appearance controls for the local preview.
class SettingsScreen extends StatelessWidget {
  const SettingsScreen({
    super.key,
    required this.memoryEnabled,
    required this.onMemoryChanged,
    required this.themeMode,
    required this.onThemeModeChanged,
  });

  final bool memoryEnabled;
  final ValueChanged<bool> onMemoryChanged;
  final ThemeMode themeMode;
  final ValueChanged<ThemeMode> onThemeModeChanged;

  Future<void> _handleMemoryChange(
    BuildContext context,
    bool shouldEnable,
  ) async {
    if (!shouldEnable) {
      onMemoryChanged(false);
      return;
    }

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        scrollable: true,
        icon: const Icon(Icons.shield_outlined),
        title: const Text('Allow long-term memory?'),
        content: const Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'With your consent, a future version may save selected '
              'preferences and brief conversation summaries to personalize '
              'later AI conversations.',
            ),
            SizedBox(height: 16),
            _ConsentPoint(
              icon: Icons.check_circle_outline_rounded,
              text: 'Memory is optional and stays off until you enable it.',
            ),
            SizedBox(height: 10),
            _ConsentPoint(
              icon: Icons.delete_outline_rounded,
              text: 'You can review or delete saved memory at any time.',
            ),
            SizedBox(height: 16),
            Text(
              'This local preview does not store real conversation memories.',
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('I understand, enable'),
          ),
        ],
      ),
    );

    if (confirmed == true) {
      onMemoryChanged(true);
    }
  }

  void _showPlaceholder(BuildContext context, String message) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

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
                        'Settings',
                        style: theme.textTheme.headlineMedium?.copyWith(
                          fontWeight: FontWeight.w700,
                          letterSpacing: -0.5,
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Control your local preview, privacy choices, and '
                      'appearance.',
                      style: theme.textTheme.bodyLarge?.copyWith(
                        color: colorScheme.onSurfaceVariant,
                        height: 1.45,
                      ),
                    ),
                    const SizedBox(height: 28),
                    _SettingsSection(
                      title: 'Memory',
                      icon: Icons.psychology_alt_outlined,
                      children: [
                        SwitchListTile.adaptive(
                          contentPadding: EdgeInsets.zero,
                          title: const Text('Long-term memory'),
                          subtitle: const Text(
                            'Requires informed consent before anything can '
                            'be remembered across conversations.',
                          ),
                          value: memoryEnabled,
                          onChanged: (value) =>
                              _handleMemoryChange(context, value),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          memoryEnabled
                              ? 'Consent is on for this preview. No real '
                                    'memory is stored yet.'
                              : 'Off — no long-term memory is allowed.',
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: colorScheme.onSurfaceVariant,
                          ),
                        ),
                        const SizedBox(height: 16),
                        Wrap(
                          spacing: 10,
                          runSpacing: 10,
                          children: [
                            OutlinedButton.icon(
                              onPressed: () => _showPlaceholder(
                                context,
                                'There are no saved memories in this local '
                                'preview.',
                              ),
                              icon: const Icon(Icons.list_alt_rounded),
                              label: const Text('View memory'),
                            ),
                            OutlinedButton.icon(
                              onPressed: () => _showPlaceholder(
                                context,
                                'Nothing was deleted because this local '
                                'preview stores no memories.',
                              ),
                              icon: const Icon(Icons.delete_outline_rounded),
                              label: const Text('Delete memory'),
                            ),
                          ],
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    _SettingsSection(
                      title: 'Appearance',
                      icon: Icons.palette_outlined,
                      children: [
                        Text(
                          'Theme',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 12),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: ThemeMode.values.map((mode) {
                            return ChoiceChip(
                              avatar: Icon(_themeIcon(mode), size: 18),
                              label: Text(_themeLabel(mode)),
                              selected: themeMode == mode,
                              onSelected: (_) => onThemeModeChanged(mode),
                            );
                          }).toList(),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    _SettingsSection(
                      title: 'Privacy',
                      icon: Icons.lock_outline_rounded,
                      children: [
                        const _InformationRow(
                          icon: Icons.mic_off_outlined,
                          title: 'No audio collection',
                          body:
                              'The mock conversation does not access your '
                              'microphone or record audio.',
                        ),
                        const SizedBox(height: 18),
                        const _InformationRow(
                          icon: Icons.cloud_off_outlined,
                          title: 'No network activity',
                          body:
                              'This milestone keeps its deterministic '
                              'experience on your device.',
                        ),
                        const SizedBox(height: 18),
                        const _InformationRow(
                          icon: Icons.manage_accounts_outlined,
                          title: 'You stay in control',
                          body:
                              'Long-term memory remains optional and must be '
                              'easy to review or delete when implemented.',
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    _DisclosureCard(colorScheme: colorScheme),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }

  static String _themeLabel(ThemeMode mode) => switch (mode) {
    ThemeMode.system => 'System',
    ThemeMode.light => 'Light',
    ThemeMode.dark => 'Dark',
  };

  static IconData _themeIcon(ThemeMode mode) => switch (mode) {
    ThemeMode.system => Icons.brightness_auto_rounded,
    ThemeMode.light => Icons.light_mode_outlined,
    ThemeMode.dark => Icons.dark_mode_outlined,
  };
}

class _SettingsSection extends StatelessWidget {
  const _SettingsSection({
    required this.title,
    required this.icon,
    required this.children,
  });

  final String title;
  final IconData icon;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Card(
      margin: EdgeInsets.zero,
      elevation: 0,
      color: colorScheme.surfaceContainerLow,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(icon, color: colorScheme.primary, size: 22),
                const SizedBox(width: 10),
                Semantics(
                  header: true,
                  child: Text(
                    title,
                    style: theme.textTheme.titleLarge?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 20),
            ...children,
          ],
        ),
      ),
    );
  }
}

class _InformationRow extends StatelessWidget {
  const _InformationRow({
    required this.icon,
    required this.title,
    required this.body,
  });

  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, color: colorScheme.onSurfaceVariant, size: 22),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: theme.textTheme.titleSmall?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 3),
              Text(
                body,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: colorScheme.onSurfaceVariant,
                  height: 1.4,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _DisclosureCard extends StatelessWidget {
  const _DisclosureCard({required this.colorScheme});

  final ColorScheme colorScheme;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Semantics(
      label: 'AI disclosure',
      child: Container(
        padding: const EdgeInsets.all(20),
        decoration: BoxDecoration(
          color: colorScheme.primaryContainer,
          borderRadius: BorderRadius.circular(24),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.auto_awesome_rounded, color: colorScheme.primary),
            const SizedBox(height: 14),
            Text(
              'AI disclosure',
              style: theme.textTheme.titleLarge?.copyWith(
                color: colorScheme.onPrimaryContainer,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              '${AppCopy.productName} is an AI companion, not a person, '
              'therapist, or emergency service. For urgent or professional '
              'help, contact appropriate local services.',
              style: theme.textTheme.bodyMedium?.copyWith(
                color: colorScheme.onPrimaryContainer,
                height: 1.45,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ConsentPoint extends StatelessWidget {
  const _ConsentPoint({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 20),
        const SizedBox(width: 10),
        Expanded(child: Text(text)),
      ],
    );
  }
}
