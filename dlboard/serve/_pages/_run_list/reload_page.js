// Clientside body for a run list's delete: draw the page again, now that the runs it drew are gone. Returns `no_update` always --
// side-effect only; the app shell's `location` it is attached to is never written.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function reloadPage(at) {
    if (at) {
        window.location.reload();
    }
    return window.dash_clientside.no_update;
}
