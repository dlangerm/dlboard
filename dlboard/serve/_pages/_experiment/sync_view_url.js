// Clientside body for `_views.py`'s URL-sync callback: whenever `STATE_VIEW_ID` changes because an
// edit branched into a new personal view (`sync_view_after_edit`, off the back of
// `_experiment_page_state.py`'s `save_page`), reflect that in the address bar with a plain
// `pushState` -- not a full navigation the way `view_navigate.js` does for an explicit picker
// switch: the edit that caused this already re-rendered everything there is to see, so there's
// nothing left to reload for, and reloading would throw that just-rendered result away.
// Declares `STATE_VIEW_ID` as both Input and Output (always returning `no_update`) purely to
// piggyback on its changes -- there's no Dash prop that means "the browser's own URL", so this is
// the same side-effect-only pattern `view_navigate.js` uses.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function syncViewUrl(viewId) {
    const url = new URL(window.location.href);
    const current = url.searchParams.get("view");
    const next = viewId === null || viewId === undefined ? null : String(viewId);
    if (current === next) {
        return window.dash_clientside.no_update;
    }
    if (next === null) {
        url.searchParams.delete("view");
    } else {
        url.searchParams.set("view", next);
    }
    window.history.pushState({}, "", url);
    return window.dash_clientside.no_update;
}
