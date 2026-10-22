// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesModalPicture(opened, run, step, data) {
    // The enlarged view's instance, shared by the chart's slider, thumbnails and the view itself.
    const instance = window.dash_clientside.callback_context.inputs_list[0].id.instance;
    if (!opened || run === null || !data) {
        window.dlImageSeriesViewer.close(instance);
    } else {
        // The picture the run shows at the slider's step: the enlarged view follows the slider, which its buttons, timeline and
        // the keyboard move. The viewer draws its title, tags and timeline, and puts the picture in place (`src`) itself once it
        // has loaded, so this has no value of its own to return.
        window.dlImageSeriesViewer.open(instance, data, run, step);
    }
    return window.dash_clientside.no_update;
}
