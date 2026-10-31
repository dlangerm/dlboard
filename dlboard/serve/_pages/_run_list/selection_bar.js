// Clientside body for a run list's action bar: shown once a row is selected, saying how many are. The table is seeded with
// its selection as `{ids: [...]}` until ag-grid reports the rows themselves, so both shapes are counted.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListSelection(selectedRows) {
    const count = Array.isArray(selectedRows) ? selectedRows.length : (selectedRows?.ids || []).length;
    return [{ display: count ? "block" : "none" }, `${count} selected`];
}
