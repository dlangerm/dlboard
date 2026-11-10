// Clientside body for `_card_grid.py`'s URL-sync callback: mirror a project page grid's filter and page into the address
// bar, so a link shows the same cards (`CardQuery.of` reads them back). The grid is named by its filter's id
// (`<grid>-filter`), and its parameters are `<grid>_q` and `<grid>_page`. A plain `replaceState` -- not `pushState` -- so
// typing in the filter doesn't flood the back button, and it doesn't re-run the page's `layout()`: the grid already shows
// this state. Only non-default state is written; other params are left alone.
// Returns `no_update` always -- side-effect only, the same pattern as `sync_pins_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncCardsUrl(search, page, filterId) {
    const grid = String(filterId).replace(/-filter$/, "");
    const params = {
        [`${grid}_q`]: search?.trim() || null,
        [`${grid}_page`]: page > 1 ? String(page) : null,
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
