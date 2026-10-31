// Clientside body for a run list's action buttons: turn a click on one into the request a server callback acts on, naming the
// rows selected in the list when it was made. The list is drawn by a callback, and Dash calls a callback listening to
// something drawn that way as it appears, though nobody clicked: this keeps that call off the server. A click with no row selected
// (the bar is hidden then, but a key press could still reach a button) asks for nothing -- except to compare the runs pinned in a
// colour, which names no runs and so needs none selected.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListAction(_clicks, selectedRows) {
    const ns = window.dash_clientside;
    const [clicked] = ns.callback_context.triggered;
    if (!clicked?.value) {
        return ns.no_update;
    }
    const button = JSON.parse(clicked.prop_id.slice(0, clicked.prop_id.lastIndexOf(".")));
    const rows = Array.isArray(selectedRows) ? selectedRows : [];
    if (rows.length === 0 && button.action !== "compare_pins") {
        return ns.no_update;
    }
    // The colour or comparison a button names rides in the request only where it names one.
    return {
        action: button.action,
        ...(button.color && { color: button.color }),
        ...(button.comparison_id && { comparison_id: Number(button.comparison_id) }),
        run_ids: rows.map((row) => row.run_id),
        at: Date.now(),
    };
}
