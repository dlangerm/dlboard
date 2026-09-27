// Clientside body for `_jump.py`'s navigation callback: go to the picked path the same way an
// in-app link does (push it onto the history, then tell Dash's page router), close the palette, and
// clear the pick so the same destination can be chosen again next time.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function jumpNavigate(path) {
    if (!path) {
        return window.dash_clientside.no_update;
    }
    if (path !== window.location.pathname) {
        window.history.pushState({}, "", path);
        window.dispatchEvent(new CustomEvent("_dashprivate_pushstate"));
    }
    return [false, null];
}
