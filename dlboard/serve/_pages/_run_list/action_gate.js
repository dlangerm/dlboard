// Clientside body for a run list's action buttons: turn a click on one into the request a server callback acts on, naming the
// rows selected in the list when it was made. The list is drawn by a callback, and Dash calls a callback listening to
// something drawn that way as it appears, though nobody clicked: this keeps that call off the server, and a click with no
// row selected (the bar is hidden then, but a key press could still reach a button) asks for nothing.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListAction(_clicks, selectedRows) {
    const ns = window.dash_clientside;
    const [clicked] = ns.callback_context.triggered;
    if (!clicked?.value || !Array.isArray(selectedRows) || selectedRows.length === 0) {
        return ns.no_update;
    }
    const button = JSON.parse(clicked.prop_id.slice(0, clicked.prop_id.lastIndexOf(".")));
    // The colour or comparison a button names rides in the request only where it names one.
    return {
        action: button.action,
        ...(button.color && { color: button.color }),
        ...(button.comparison_id && { comparison_id: Number(button.comparison_id) }),
        run_ids: selectedRows.map((row) => row.run_id),
        at: Date.now(),
    };
}
