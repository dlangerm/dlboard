// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesModalOverlay(opened, src, data) {
    const noUpdate = window.dash_clientside.no_update;
    if (!opened || !src) {
        return noUpdate;
    }
    // The modal is kept mounted (`keepMounted`), so its layer exists already when it opens. The layer's own style is
    // never changed here: the drawing is a side effect.
    const instance = window.dash_clientside.callback_context.inputs_list[0].id.instance;
    const layer = document.getElementById(
        JSON.stringify({ instance, type: "image-series-modal-overlay" }, ["instance", "type"]),
    );
    if (layer) {
        window.dlImageSeriesOverlay.show(layer, data.overlays?.[src] ?? []);
    }
    return noUpdate;
}
