// Clientside body for a page's tab URL-sync callback (the project page's, the admin page's): mirror the page's tab into
// the address bar as `?tab=`, so a link or a reload opens the page on it (the page's `layout()` reads it back). A plain
// `replaceState`, as `sync_cards_url.js` does: switching tabs doesn't flood the back button, and doesn't re-run the page's
// `layout()`, which already drew every tab. Other params are left alone.
// Returns `no_update` always -- side-effect only.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncTabUrl(tab) {
    if (!tab) {
        return window.dash_clientside.no_update;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    if (url.href !== window.location.href) {
        window.history.replaceState({}, "", url);
    }
    return window.dash_clientside.no_update;
}
