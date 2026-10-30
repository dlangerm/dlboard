// Clientside body for `_pinned_table.py`'s URL-sync callback: mirror the pinned-runs filters and page into the address bar
// so the link can be shared (`PinFilter` is the server-side reader of these params). A plain `replaceState` -- not
// `pushState` -- so typing in the search box doesn't flood the back button, and it doesn't re-run the page's `layout()`:
// the list already shows this state. Only non-default state is written; other params are left alone.
// Returns `no_update` always -- side-effect only, the same pattern as `sync_compare_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncPinsUrl(colors, pinnedBy, experiment, search, page) {
    const params = {
        pins_color: colors?.length ? colors.join(",") : null,
        pins_by: pinnedBy?.length ? pinnedBy.join(",") : null,
        pins_exp: experiment || null,
        pins_q: search || null,
        pins_page: page > 1 ? String(page) : null,
    };
    const url = new URL(window.location.href);
    for (const [name, value] of Object.entries(params)) {
        if (value === null) {
            url.searchParams.delete(name);
        } else {
            url.searchParams.set(name, value);
        }
    }
    if (url.href !== window.location.href) {
        window.history.replaceState({}, "", url);
    }
    return window.dash_clientside.no_update;
}
