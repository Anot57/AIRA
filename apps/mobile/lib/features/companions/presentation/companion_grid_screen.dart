import 'package:flutter/material.dart';

import '../../../core/constants/app_copy.dart';
import '../data/companion_catalog.dart';
import '../domain/companion.dart';
import '../domain/companion_filter.dart';
import 'widgets/companion_card.dart';

/// Browses the locally bundled catalog of fictional AI companions.
class CompanionGridScreen extends StatefulWidget {
  const CompanionGridScreen({
    super.key,
    required this.catalog,
    required this.onCompanionSelected,
  });

  final CompanionCatalog catalog;
  final ValueChanged<Companion> onCompanionSelected;

  @override
  State<CompanionGridScreen> createState() => _CompanionGridScreenState();
}

class _CompanionGridScreenState extends State<CompanionGridScreen> {
  late Future<List<Companion>> _companionsFuture;
  CompanionFilter? _selectedFilter;

  @override
  void initState() {
    super.initState();
    _companionsFuture = widget.catalog.load();
  }

  @override
  void didUpdateWidget(CompanionGridScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.catalog, widget.catalog)) {
      _companionsFuture = widget.catalog.load();
      _selectedFilter = null;
    }
  }

  void _retryLoading() {
    setState(() {
      _companionsFuture = widget.catalog.load();
    });
  }

  void _selectFilter(CompanionFilter? filter) {
    setState(() {
      _selectedFilter = filter;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            final basePadding = constraints.maxWidth < 360 ? 12.0 : 16.0;
            final horizontalPadding = constraints.maxWidth > 720
                ? (constraints.maxWidth - 688) / 2
                : basePadding;
            final textScale = MediaQuery.textScalerOf(context).scale(1);
            final extraCardHeight = ((textScale - 1).clamp(0.0, 2.0)) * 84;

            return FutureBuilder<List<Companion>>(
              future: _companionsFuture,
              builder: (context, snapshot) {
                final companions = snapshot.data ?? const <Companion>[];
                final visibleCompanions = _selectedFilter == null
                    ? companions
                    : companions
                          .where(
                            (companion) =>
                                companion.matchesFilter(_selectedFilter!),
                          )
                          .toList(growable: false);

                return CustomScrollView(
                  key: PageStorageKey<String>(
                    snapshot.hasData
                        ? 'companion-catalog-ready'
                        : 'companion-catalog-loading',
                  ),
                  physics: const BouncingScrollPhysics(
                    parent: AlwaysScrollableScrollPhysics(),
                  ),
                  slivers: [
                    SliverPadding(
                      padding: EdgeInsets.fromLTRB(
                        horizontalPadding,
                        16,
                        horizontalPadding,
                        0,
                      ),
                      sliver: const SliverToBoxAdapter(child: _CatalogHeader()),
                    ),
                    if (snapshot.hasData && companions.isNotEmpty) ...[
                      SliverPadding(
                        padding: const EdgeInsets.only(top: 18),
                        sliver: SliverToBoxAdapter(
                          child: _FilterStrip(
                            horizontalPadding: horizontalPadding,
                            selectedFilter: _selectedFilter,
                            onSelected: _selectFilter,
                          ),
                        ),
                      ),
                      SliverPadding(
                        padding: EdgeInsets.fromLTRB(
                          horizontalPadding,
                          18,
                          horizontalPadding,
                          12,
                        ),
                        sliver: SliverToBoxAdapter(
                          child: _ResultCount(
                            count: visibleCompanions.length,
                            selectedFilter: _selectedFilter,
                          ),
                        ),
                      ),
                    ],
                    if (snapshot.connectionState == ConnectionState.waiting)
                      const SliverFillRemaining(
                        hasScrollBody: false,
                        child: _LoadingState(),
                      )
                    else if (snapshot.hasError)
                      SliverFillRemaining(
                        hasScrollBody: false,
                        child: _ErrorState(onRetry: _retryLoading),
                      )
                    else if (companions.isEmpty)
                      const SliverFillRemaining(
                        hasScrollBody: false,
                        child: _EmptyCatalogState(),
                      )
                    else if (visibleCompanions.isEmpty)
                      SliverFillRemaining(
                        hasScrollBody: false,
                        child: _NoFilterMatchesState(
                          filterLabel: _selectedFilter!.label,
                          onShowAll: () => _selectFilter(null),
                        ),
                      )
                    else
                      SliverPadding(
                        padding: EdgeInsets.fromLTRB(
                          horizontalPadding,
                          0,
                          horizontalPadding,
                          28,
                        ),
                        sliver: SliverGrid(
                          gridDelegate:
                              SliverGridDelegateWithFixedCrossAxisCount(
                                crossAxisCount: 2,
                                crossAxisSpacing: 12,
                                mainAxisSpacing: 12,
                                mainAxisExtent: 296 + extraCardHeight,
                              ),
                          delegate: SliverChildBuilderDelegate((
                            context,
                            index,
                          ) {
                            final companion = visibleCompanions[index];
                            return CompanionCard(
                              key: ValueKey<String>(
                                'companion-card-${companion.id}',
                              ),
                              companion: companion,
                              onTap: () =>
                                  widget.onCompanionSelected(companion),
                            );
                          }, childCount: visibleCompanions.length),
                        ),
                      ),
                  ],
                );
              },
            );
          },
        ),
      ),
    );
  }
}

class _CatalogHeader extends StatelessWidget {
  const _CatalogHeader();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Semantics(
      container: true,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(28),
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [
              colors.primaryContainer.withValues(alpha: 0.88),
              Color.alphaBlend(
                colors.tertiary.withValues(alpha: 0.12),
                colors.surfaceContainer,
              ),
            ],
          ),
          border: Border.all(color: colors.outlineVariant),
        ),
        child: Padding(
          padding: const EdgeInsets.all(22),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Container(
                    width: 42,
                    height: 42,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      gradient: LinearGradient(
                        colors: [colors.primary, colors.tertiary],
                      ),
                    ),
                    child: Icon(
                      Icons.auto_awesome_rounded,
                      color: colors.onPrimary,
                      semanticLabel: '${AppCopy.productName} AI companions',
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      '${AppCopy.productName} companions',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: theme.textTheme.titleMedium?.copyWith(
                        color: colors.onPrimaryContainer,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 10,
                      vertical: 6,
                    ),
                    decoration: BoxDecoration(
                      color: colors.inverseSurface,
                      borderRadius: BorderRadius.circular(999),
                    ),
                    child: Text(
                      'AI',
                      style: theme.textTheme.labelSmall?.copyWith(
                        color: colors.onInverseSurface,
                        fontWeight: FontWeight.w900,
                        letterSpacing: 0.8,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 24),
              Semantics(
                header: true,
                child: Text(
                  'Choose your conversation style',
                  style: theme.textTheme.headlineSmall?.copyWith(
                    color: colors.onPrimaryContainer,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.4,
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Every profile is a fictional AI companion with a distinct '
                'personality and conversation style.',
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: colors.onPrimaryContainer.withValues(alpha: 0.86),
                  height: 1.45,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _FilterStrip extends StatelessWidget {
  const _FilterStrip({
    required this.horizontalPadding,
    required this.selectedFilter,
    required this.onSelected,
  });

  final double horizontalPadding;
  final CompanionFilter? selectedFilter;
  final ValueChanged<CompanionFilter?> onSelected;

  @override
  Widget build(BuildContext context) {
    final filters = <CompanionFilter?>[null, ...CompanionFilter.values];

    return Semantics(
      container: true,
      label: 'Filter companions by personality',
      child: SizedBox(
        height: 48,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          padding: EdgeInsets.symmetric(horizontal: horizontalPadding),
          itemCount: filters.length,
          separatorBuilder: (context, index) => const SizedBox(width: 8),
          itemBuilder: (context, index) {
            final filter = filters[index];
            final label = filter?.label ?? 'All';
            final selected = filter == selectedFilter;

            return FilterChip(
              key: ValueKey<String>('filter-$label'),
              label: Text(label),
              selected: selected,
              showCheckmark: false,
              avatar: selected
                  ? const Icon(Icons.check_rounded, size: 17)
                  : null,
              onSelected: (_) => onSelected(filter),
              tooltip: selected
                  ? '$label filter selected'
                  : 'Show $label companions',
            );
          },
        ),
      ),
    );
  }
}

class _ResultCount extends StatelessWidget {
  const _ResultCount({required this.count, required this.selectedFilter});

  final int count;
  final CompanionFilter? selectedFilter;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final noun = count == 1 ? 'AI companion' : 'AI companions';

    return Row(
      children: [
        Expanded(
          child: Text(
            selectedFilter?.label ?? 'All companions',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.titleMedium?.copyWith(
              fontWeight: FontWeight.w800,
            ),
          ),
        ),
        const SizedBox(width: 12),
        Text(
          '$count $noun',
          key: const ValueKey<String>('companion-result-count'),
          style: theme.textTheme.labelMedium?.copyWith(
            color: colors.onSurfaceVariant,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}

class _LoadingState extends StatelessWidget {
  const _LoadingState();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Semantics(
        label: 'Loading AI companions',
        child: const CircularProgressIndicator(),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 360),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                Icons.cloud_off_rounded,
                color: colors.onSurfaceVariant,
                size: 40,
                semanticLabel: 'Catalog unavailable',
              ),
              const SizedBox(height: 14),
              Text(
                'The local companion catalog could not be opened.',
                textAlign: TextAlign.center,
                style: theme.textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              Text(
                'Nothing was sent over the network. Try loading the bundled '
                'catalog again.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: colors.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 20),
              FilledButton.tonalIcon(
                key: const ValueKey<String>('retry-companion-catalog'),
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Try again'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _EmptyCatalogState extends StatelessWidget {
  const _EmptyCatalogState();

  @override
  Widget build(BuildContext context) {
    return const _MessageState(
      icon: Icons.auto_awesome_outlined,
      title: 'No companions available',
      message: 'The local AI companion catalog is empty.',
    );
  }
}

class _NoFilterMatchesState extends StatelessWidget {
  const _NoFilterMatchesState({
    required this.filterLabel,
    required this.onShowAll,
  });

  final String filterLabel;
  final VoidCallback onShowAll;

  @override
  Widget build(BuildContext context) {
    return _MessageState(
      icon: Icons.tune_rounded,
      title: 'No $filterLabel matches',
      message: 'Choose another personality filter to keep exploring.',
      action: TextButton(onPressed: onShowAll, child: const Text('Show all')),
    );
  }
}

class _MessageState extends StatelessWidget {
  const _MessageState({
    required this.icon,
    required this.title,
    required this.message,
    this.action,
  });

  final IconData icon;
  final String title;
  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 40,
              color: colors.onSurfaceVariant,
              semanticLabel: title,
            ),
            const SizedBox(height: 14),
            Text(
              title,
              textAlign: TextAlign.center,
              style: theme.textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            Text(
              message,
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium?.copyWith(
                color: colors.onSurfaceVariant,
              ),
            ),
            if (action case final action?) ...[
              const SizedBox(height: 12),
              action,
            ],
          ],
        ),
      ),
    );
  }
}
