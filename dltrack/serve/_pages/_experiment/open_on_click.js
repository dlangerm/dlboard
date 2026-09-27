// Clientside body for opening a modal from a menu item (`_views.py`): any click opens it; closing is
// the modal's own business.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function openOnClick(nClicks) {
    return nClicks ? true : window.dash_clientside.no_update;
}
