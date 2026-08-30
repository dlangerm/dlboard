// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartParamClear(n) {
    return n ? null : window.dash_clientside.no_update;
}
