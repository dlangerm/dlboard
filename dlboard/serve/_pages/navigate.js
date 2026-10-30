// Clientside body for `register_navigate`: go to the address a server callback stored, the way an in-app link does -- pushed
// onto the history and announced to Dash's page router, which draws the page (the router's own location does not follow the
// app's). Not a hard load: that would be undone by the page's address bar mirror (`paging_side_effects.js`), which writes
// the address as the page last knew it. Returns `no_update` always -- side-effect only; the empty store a page is drawn with
// goes nowhere.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function navigate(destination) {
    if (destination) {
        window.history.pushState({}, "", destination);
        window.dispatchEvent(new CustomEvent("_dashprivate_pushstate"));
    }
    return window.dash_clientside.no_update;
}
