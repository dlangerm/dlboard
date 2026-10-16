// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesScrub(step, data) {
    if (!data) {
        return [window.dash_clientside.no_update, window.dash_clientside.no_update];
    }
    const runIds = Object.keys(data.per_run_steps);
    const srcs = [];
    const captions = [];
    runIds.forEach((rid) => {
        const steps = data.per_run_steps[rid];
        let chosen = steps[0];
        for (let i = 0; i < steps.length; i++) {
            if (steps[i] <= step) {
                chosen = steps[i];
            } else {
                break;
            }
        }
        srcs.push(data.per_run_urls[rid][String(chosen)]);
        const runCaptions = data.per_run_captions ? data.per_run_captions[rid] : null;
        captions.push(runCaptions ? runCaptions[String(chosen)] || "" : "");
    });
    // Only a slider the user moved has a thumbnail to mark: the initial call has no trigger.
    const instance = window.dash_clientside.callback_context.triggered_id?.instance;
    if (instance !== undefined) {
        // A picture that is about to change gets a spinner over its thumbnail until the new picture has loaded.
        // Only the picture the thumbnail shows when a load ends clears it, so a picture replaced in the meantime
        // changes nothing.
        const domId = (type, rid) =>
            JSON.stringify({ instance, run: rid, type }, ["instance", "run", "type"]);
        runIds.forEach((rid, i) => {
            const thumb = document.getElementById(domId("image-series-thumb", rid));
            const img = thumb?.querySelector("img");
            if (!img || img.getAttribute("src") === srcs[i]) {
                return;
            }
            const settle = () => {
                if (img.getAttribute("src") === srcs[i]) {
                    thumb.removeAttribute("data-dl-loading");
                }
            };
            thumb.setAttribute("data-dl-loading", "");
            img.onload = settle;
            img.onerror = settle;
        });
    }
    return [srcs, captions];
}
