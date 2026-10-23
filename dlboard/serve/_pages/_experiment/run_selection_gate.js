// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runSelectionGate(selectedRows, drawn) {
    // ag-grid echoes `selectedRows` as it mounts (first as the `{ids}` shorthand the table was seeded with, then as the
    // rows) without anyone having chosen anything. Only a selection that differs from the runs drawn is worth a server round trip.
    const selected = Array.isArray(selectedRows)
        ? selectedRows.map((row) => row.run_id)
        : (selectedRows?.ids || []).map(Number);
    const sorted = [...selected].sort((a, b) => a - b);
    const same =
        sorted.length === drawn.length && [...drawn].sort((a, b) => a - b).every((id, i) => id === sorted[i]);
    return same ? window.dash_clientside.no_update : sorted;
}
