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
    return [srcs, captions];
}
