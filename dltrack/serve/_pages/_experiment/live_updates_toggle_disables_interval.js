// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function liveUpdatesToggleDisablesInterval(checked) {
    // Purely client-side: flipping the switch shouldn't need a server round trip just to pause a
    // timer. `dcc.Interval.disabled` stops it firing at all, so `poll_for_updates` (and the DB read
    // behind it) genuinely stops running while unchecked, not just "runs and gets ignored".
    return !checked;
}
