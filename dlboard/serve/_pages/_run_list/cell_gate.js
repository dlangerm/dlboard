// Clientside body for the cells of a run list that ask for something (an eye to draw or hide a run, a pin editor to pin it): a
// cell renderer hands what it asks for to `setData` (`{action, run_ids}`, and the colour or comparison it names), which the grid reports as `cellRendererData`,
// and this adds when it was asked, so asking twice is two requests. Returns `no_update` for the empty value ag-grid mounts with.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function runListCell(cell) {
    return cell?.value ? { ...cell.value, at: Date.now() } : window.dash_clientside.no_update;
}
