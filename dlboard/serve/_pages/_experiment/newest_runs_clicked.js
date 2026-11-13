// Clientside body for the "Newest N" button's click (`_runs_table.py`): only a real click reaches the server. The button is
// drawn again with the run list's head after every draw or hide, and Dash calls a callback whose input was just drawn,
// though nobody clicked anything. A time, so that every click is a change, whatever the button counted before.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function newestRunsClicked(nClicks) {
    return nClicks ? Date.now() : window.dash_clientside.no_update;
}
