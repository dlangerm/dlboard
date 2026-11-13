// Moves every other step slider of a synced panel along with the one dragged, whatever chart each is of (`_step_slider.py`).
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function stepSliderLock(values, ids, marks) {
    // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
    const domId = (id) => JSON.stringify(id, Object.keys(id).sort());
    // The slider values this script set itself, by slider: their own change events are not the
    // user's, and passing them on would bounce them back and forth between sliders whose steps differ.
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlStepSliderLockedTo ??= {};
    const lockedTo = window.dlStepSliderLockedTo;
    const noUpdate = window.dash_clientside.no_update;
    const dragged = window.dash_clientside.callback_context.triggered_id;
    const draggedIndex = dragged ? ids.findIndex((id) => domId(id) === domId(dragged)) : -1;
    if (draggedIndex < 0) {
        return noUpdate;
    }
    if (lockedTo[domId(dragged)] === values[draggedIndex]) {
        delete lockedTo[domId(dragged)];
        return noUpdate;
    }
    const element = (id) => document.getElementById(domId(id));
    // The panel sets this on its charts' container when its "sync" setting is on.
    const panel = element(ids[draggedIndex])?.closest("[data-dl-panel-sync]");
    if (!panel) {
        return noUpdate;
    }
    ids.forEach((id, i) => {
        if (i === draggedIndex || !panel.contains(element(id))) {
            return;
        }
        // The latest step this slider has at or before the dragged one, like a chart does for a run
        // that has not logged at that step: the first one if it has none yet.
        const steps = marks[i].map((mark) => mark.value).sort((a, b) => a - b);
        const target = steps.filter((step) => step <= values[draggedIndex]).pop() ?? steps[0];
        if (target !== values[i]) {
            lockedTo[domId(id)] = target;
            window.dash_clientside.set_props(id, { value: target });
        }
    });
    return noUpdate;
}
