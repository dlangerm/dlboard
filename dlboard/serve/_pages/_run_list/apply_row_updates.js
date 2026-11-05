// Clientside body for a run list's row updates (`_rows.py`), in place in the grid that holds them, so the list is not drawn
// again and keeps its scroll, sort, selection and filters: the grid's complete rows (`replace`, which ag-grid reconciles by
// row id: rows added, changed and removed, in the order given) and/or the fields of some rows (`rows`) to set. Then the
// grid's filters are run again, since a row that no longer matches one (a run unpinned while the pin filter shows only
// pinned runs) must leave the view.
// Returns `no_update` always -- side-effect only, the same pattern as `sync_view_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function applyRowUpdates(update) {
    const api = update ? window.dash_ag_grid.getApi(update.grid) : null;
    if (!api) {
        return window.dash_clientside.no_update;
    }
    if (update.replace) {
        api.setGridOption("rowData", update.replace);
        // A cell is drawn again only if what it shows changed, and a name's link is drawn from fields it does not show (the
        // view it opens in): so they all are, whatever the rows changed in.
        api.refreshCells({ force: true });
    }
    const redrawn = [];
    for (const { run_id, values } of update.rows) {
        const node = api.getRowNode(String(run_id));
        if (!node) {
            continue;
        }
        for (const [field, value] of Object.entries(values)) {
            // `setDataValue` takes only a field with a column: one a cell draws from without showing it (`_kept`, the lock
            // after a kept run's name) has none, and is set on the row's data, for the refresh below to draw.
            if (api.getColumn(field)) {
                node.setDataValue(field, value);
            } else {
                node.data[field] = value;
                redrawn.push(node);
            }
        }
    }
    // A cell is drawn again only if its own value changed: a name's lock is drawn from another field. Only those rows, so a
    // cell with something open in it (a pin's editor) is not drawn again under it.
    if (redrawn.length) {
        api.refreshCells({ rowNodes: redrawn, force: true });
    }
    api.onFilterChanged();
    // A pin changed, not the selection: what the Unpin menu offers is what the selected rows hold now (`unpin_colors.js`).
    window.dlSetUnpinColors?.(api.getSelectedRows());
    return window.dash_clientside.no_update;
}
