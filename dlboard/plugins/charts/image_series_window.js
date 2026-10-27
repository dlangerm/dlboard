// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesWindow(step, loaded) {
    // A chart that read every picture has no window; inside the one it has, there is nothing to ask the server for.
    if (!loaded || step === null || step === undefined || (step >= loaded.first && step <= loaded.last)) {
        return window.dash_clientside.no_update;
    }
    return { step };
}
