// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function tableChartPage(info, id) {
    // Remembers the page a table chart's grid is on, and puts a freshly mounted one (a live update replaces
    // the whole chart) back on it. A grid reports nothing until it has rows, so a call with no `info`
    // is a grid mounting.
    const noUpdate = window.dash_clientside.no_update;
    const key = `dl-table-page:${JSON.stringify(id)}`;
    if (!window.dlTablesMounting) {
        window.dlTablesMounting = new Set();
    }
    const mounting = window.dlTablesMounting;
    if (!info) {
        mounting.add(key);
        return noUpdate;
    }
    let saved = 0;
    try {
        saved = Number(window.sessionStorage.getItem(key)) || 0;
    } catch {
        // Storage can be blocked: the table then just starts on its first page.
    }
    if (mounting.delete(key) && saved > 0 && info.currentPage !== saved) {
        return saved;
    }
    try {
        window.sessionStorage.setItem(key, String(info.currentPage));
    } catch {
        // See above.
    }
    return noUpdate;
}
