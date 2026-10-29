"""Bounded, browser-session-owned reuse of immutable dataset versions."""
from .pipeline import read_and_clean_csv_bytes, first_seen_customer_rows, weekly_opportunity_ytd
from .zip_maps import zip_code_summaries


def latest_dashboard(cache, store, status, settings):
    existing = cache.get('dashboard')
    if existing is not None and existing['status'].version == status.version:
        return existing, None
    master, leads, actual_status = store.downloads()
    rows = read_and_clean_csv_bytes(master, settings, 'Master dataset', allow_empty=True)
    lead_rows = first_seen_customer_rows(rows, settings)
    dashboard = dict(master=master, leads=leads, status=actual_status,
                     lead_rows=lead_rows, zip_summary=zip_code_summaries(rows, lead_rows, settings),
                     has_rows=bool(rows))
    cache.clear()
    cache['dashboard'] = dashboard
    # Rows are returned only for reuse during this render; do not retain the
    # entire parsed master (hundreds of MB) in session state between reruns.
    return dashboard, rows


def imported_dashboard(cache, result, status):
    cache.clear()
    cache['dashboard'] = dict(master=result.master_csv, leads=result.new_leads_csv,
        status=status, lead_rows=result.new_lead_rows,
        zip_summary=(result.zip_year, result.zip_revenue_rows, result.zip_new_lead_rows),
        has_rows=True)


def weekly_charts(cache, dashboard, settings, statuses):
    key = (dashboard['status'].version, tuple(sorted(statuses)))
    if cache.get('chart_key') != key:
        cache['charts'] = {category: weekly_opportunity_ytd(
            dashboard['lead_rows'], category, settings, set(statuses))
            for category in ('Plumbing', 'HVAC')}
        cache['chart_key'] = key
    return cache['charts']
