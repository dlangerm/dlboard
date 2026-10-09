// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function panelGridColumns(columns) {
    // A grid's column count is only CSS, so it is applied here, the instant the number changes,
    // instead of waiting for the server to rebuild every chart in the panel. Cleared or partial
    // input arrives as null or ""; the grid keeps what it had until there is a whole number.
    return Number.isInteger(columns) ? columns : window.dash_clientside.no_update;
}
