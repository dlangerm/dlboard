// Clientside body for the run list's page trigger (`_runs_table.py`): pass the page on to the run list unless the action that
// wrote it already brought the list up to date (a run drawn or hidden writes its rows and head itself), or the page is a
// comparison's, which has no such list. Each would otherwise cost a server round trip that reads the runs, their
// hyperparameters and pins only to find nothing to change.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListGate(pageJson, handled) {
    const noUpdate = window.dash_clientside.no_update;
    if (!pageJson || pageJson === handled || JSON.parse(pageJson).experiment_id === null) {
        return noUpdate;
    }
    return pageJson;
}
