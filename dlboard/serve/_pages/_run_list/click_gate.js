// Clientside body for a run list's delete confirmation: turn a click into a request a server callback can listen to instead of the
// button. Dash calls a server callback listening to a button as the page draws it, though nobody clicked, when the callback also
// writes the app shell's `location`: a round trip the page waits on. This one is the browser's own, and returns `no_update` unless
// there was a click.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function clickGate(clicks) {
    return clicks ? Date.now() : window.dash_clientside.no_update;
}
