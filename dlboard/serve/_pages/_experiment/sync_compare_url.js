// Clientside body for `_run_compare.py`'s URL-sync callback: mirror the compare modal's state into
// the address bar so the link can be shared (`CompareQuery` is the server-side reader of these
// params). A plain `replaceState` -- not `pushState` -- so typing in the search box doesn't flood
// the back button, and it doesn't re-run the page's `layout()`: the modal already shows this state.
// Only non-default state is written (`compare_mode` when "all", `compare_q` when non-empty), and
// every param goes away with the modal; other params (`view`, `chart`) are left alone.
// Returns `no_update` always -- side-effect only, the same pattern as `sync_view_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncCompareUrl(opened, runs, mode, search) {
    const open = opened && runs && runs.length > 0;
    const params = {
        compare: open ? runs.join(",") : null,
        compare_mode: open && mode === "all" ? mode : null,
        compare_q: open && search ? search : null,
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
