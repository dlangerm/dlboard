// Clientside body for `_experiment_page_state.py`'s paging URL-sync callback: mirror which page of
// panels and charts is showing into the address bar, so the link can be shared or bookmarked
// (`Paging` is the server-side reader of these params, via `layout()`). A plain `replaceState` --
// not `pushState` -- so paging doesn't flood the back button, and it doesn't re-run the page's
// `layout()`: the page already shows this state. `data` is the serialized `Paging`; its `query` maps
// every param it manages to its value, or "" when that is the default and belongs out of the URL.
// Other params (`view`, `chart`, `compare*`) are left alone. Returns `no_update` always -- side-effect
// only, the same pattern as `sync_compare_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncPagingUrl(data) {
    const url = new URL(window.location.href);
    for (const [name, value] of Object.entries(JSON.parse(data).query)) {
        if (value) {
            url.searchParams.set(name, value);
        } else {
            url.searchParams.delete(name);
        }
    }
    if (url.href !== window.location.href) {
        window.history.replaceState({}, "", url);
    }
    return window.dash_clientside.no_update;
}
