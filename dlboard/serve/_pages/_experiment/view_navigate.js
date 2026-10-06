// Clientside body for `_views.py`'s view-switching callback: picking a view in the header's picker
// navigates to this page with `?view=<id>` (or without it, for the shared view) the way an in-app
// link does -- push it onto the history, then tell Dash's page router -- which re-renders the page
// as that view (and sets `STATE_VIEW_ID` itself). Returns nothing: the navigation is the whole point.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function viewNavigate(value, currentViewId, suppress) {
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
    url.search = "";
    if (selected !== null) {
        url.searchParams.set("view", selected);
    }
    window.history.pushState({}, "", url);
    window.dispatchEvent(new CustomEvent("_dashprivate_pushstate"));
    return [window.dash_clientside.no_update, window.dash_clientside.no_update];
}
