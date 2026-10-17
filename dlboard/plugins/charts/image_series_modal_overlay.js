// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesModalOverlay(opened, src, data) {
    const noUpdate = window.dash_clientside.no_update;
    if (!opened || !src) {
        return noUpdate;
    }
    // The modal's instance, shared by its picture and its layer. The modal is kept mounted (`keepMounted`), so both
    // exist already when it opens. The layer's own style is never changed here: the drawing is a side effect.
    const instance = window.dash_clientside.callback_context.inputs_list[0].id.instance;
    const domId = (type) => JSON.stringify({ instance, type }, ["instance", "type"]);
    const layer = document.getElementById(domId("image-series-modal-overlay"));
    const picture = document.getElementById(domId("image-series-modal-img"));
    if (!layer || !picture) {
        return noUpdate;
    }
    const img = picture.tagName === "IMG" ? picture : picture.querySelector("img");
    if (!img) {
        return noUpdate;
    }
    window.dlImageSeriesOverlay.show(layer, img, src, data.overlays?.[src] ?? []);
    return noUpdate;
}
