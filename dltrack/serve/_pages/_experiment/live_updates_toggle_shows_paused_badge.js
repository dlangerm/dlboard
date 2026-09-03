// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function liveUpdatesToggleShowsPausedBadge(checked) {
    // A plain visibility swap between two sibling badges -- not reconstructing a `dmc.Badge`'s
    // rendered shape here, and not a second callback writing the same `children` another one owns.
    return checked ? [{ display: "" }, { display: "none" }] : [{ display: "none" }, { display: "" }];
}
