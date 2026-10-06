// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function liveSwitchState(checked) {
    // Both effects of the "Live updates" switch, purely client-side, off one shared `checked`
    // Input -- merged into one callback/file rather than two, since neither has a reason to fire
    // independently of the other.
    //
    // `dcc.Interval.disabled` stops it firing at all, so `poll_for_updates` (and the DB read behind
    // it) genuinely stops running while unchecked, not just "runs and gets ignored".
    //
    // The paused badge is a plain visibility swap with the live-status badge (a sibling div toggled
    // by CSS `display`) -- not a second callback writing that div's own `children`, which
    // `poll_for_updates` stays the only owner of.
    return [!checked, checked ? {} : { display: "none" }, checked ? { display: "none" } : {}];
}
