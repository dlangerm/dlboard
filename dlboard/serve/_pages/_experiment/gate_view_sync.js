// Clientside body for `_views.py`'s picker sync: pass on the page the browser holds, but only an experiment's, and only one
// that landed in another view than the picker shows (an edit that branched into a view of its own). A comparison is not a
// view of anything, so it has no picker, share toggle or "saves to a copy" badge for the sync to update, and a server
// callback with outputs missing from the page is an error in the browser. An edit that stayed in its view, which is nearly
// every one, has nothing to sync: checked here, it costs no round trip.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function gateViewSync(pageJson, currentViewId) {
    const noUpdate = window.dash_clientside.no_update;
    if (!pageJson) {
        return noUpdate;
    }
    const page = JSON.parse(pageJson);
    if (page.experiment_id === null) {
        return noUpdate;
    }
    // A personal view is the page's own id; the shared page is none (`sync_view_after_edit`'s `effective`).
    const landedIn = page.owner_id === null ? null : page.id;
    return landedIn === (currentViewId ?? null) ? noUpdate : pageJson;
}
