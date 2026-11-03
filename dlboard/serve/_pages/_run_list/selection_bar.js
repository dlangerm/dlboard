// Clientside body for a run list's action bar: shown once a row is selected, saying how many are. The table is seeded with
// its selection as `{ids: [...]}` until ag-grid reports the rows themselves, so both shapes are counted. The Unpin menu of the
// bar is told what the selected rows hold (`unpin_colors.js`).
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListSelection(selectedRows) {
    const count = Array.isArray(selectedRows) ? selectedRows.length : (selectedRows?.ids || []).length;
    window.dlSetUnpinColors?.(selectedRows);
    return [{ display: count ? "block" : "none" }, `${count} selected`];
}
