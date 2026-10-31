// Clientside body for `app.py`'s navbar-state callback: applies the persisted collapsed/width state
// (from localStorage) and hides the navbar -- and its toggle -- entirely on pages that have nothing
// to put in it (home, admin: anything outside a project). Decided from the URL alone, so it runs in
// the browser as the page mounts rather than waiting on a server round trip.
// Width bounds are `app.py`'s `_NAVBAR_WIDTH`, which `navbar_resize.js` is served with as `window.dlNavbarWidth`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function navbarState(pathname, collapsed, width) {
    const bounds = window.dlNavbarWidth;
    const hasContent = /^\/(project|experiment|comparison)\//.test(pathname || "");
    const hidden = !hasContent || Boolean(collapsed);
    return [
        {
            width: width ? Math.min(bounds.max, Math.max(bounds.min, width)) : bounds.default,
            breakpoint: "sm",
            collapsed: { mobile: hidden, desktop: hidden },
        },
        hasContent ? {} : { display: "none" },
    ];
}
