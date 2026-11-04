// Clientside body for `_views.py`'s view-switching callback: picking a view in the header's picker
// navigates to this page with `?view=<id>` (or without it, for the shared view) the way an in-app
// link does -- push it onto the history, then tell Dash's page router -- which re-renders the page
// as that view (and sets `STATE_VIEW_ID` itself). Returns nothing: the navigation is the whole point.
// The page's other state is the old view's (which panels are open, which chart page, a chart's link), so it
// goes; but `?run=` is the run the viewer came to look at, whichever view draws it, so it stays.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function viewNavigate(value, currentViewId, suppress, routerSearch) {
    // `sync_view_after_edit` (an edit branching into a view of your own) writes this same
    // `value` prop to reflect where the edit landed -- indistinguishable, as a prop change, from
    // an actual pick. It arms this flag right beside that write so this one change is skipped
    // (the edit already put `STATE_VIEW_ID`/the URL where they belong) instead of navigating a
    // second time out from under it.
    if (suppress) {
        return [window.dash_clientside.no_update, false];
    }
    const selected = value === "shared" ? null : value;
    if (String(selected) === String(currentViewId)) {
        return [window.dash_clientside.no_update, window.dash_clientside.no_update];
    }
    const url = new URL(window.location.href);
    const run = url.searchParams.get("run");
    url.search = "";
    if (selected !== null) {
        url.searchParams.set("view", selected);
    }
    if (run !== null) {
        url.searchParams.set("run", run);
    }
    if (url.search === routerSearch) {
        // The router still holds this address: the edit that branched into the view being left moved the address bar
        // (`sync_view_url.js`) without telling it, so pushing this one would read to it as no change and draw nothing.
        window.location.assign(url);
        return [window.dash_clientside.no_update, window.dash_clientside.no_update];
    }
    window.history.pushState({}, "", url);
    window.dispatchEvent(new CustomEvent("_dashprivate_pushstate"));
    return [window.dash_clientside.no_update, window.dash_clientside.no_update];
}
