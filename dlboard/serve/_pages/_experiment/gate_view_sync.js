// Clientside body for `_views.py`'s picker sync: pass on the page the browser holds, but only an experiment's. A comparison is
// not a view of anything, so it has no picker, share toggle or "saves to a copy" badge for the sync to update, and a server
// callback with outputs missing from the page is an error in the browser.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function gateViewSync(pageJson) {
    return pageJson && JSON.parse(pageJson).experiment_id !== null
        ? pageJson
        : window.dash_clientside.no_update;
}
