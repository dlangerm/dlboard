// Clientside body for `app.py`'s navbar-toggle callback: flips the persisted collapsed state.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function navbarToggle(nClicks, collapsed) {
    return nClicks ? !collapsed : window.dash_clientside.no_update;
}
