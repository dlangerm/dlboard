// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function hparamColsChanged(value, applied) {
    const a = [...(value || [])].sort();
    const b = [...(applied || [])].sort();
    const changed = a.length !== b.length || a.some((v, i) => v !== b[i]);
    return changed ? { flexShrink: 0 } : { display: "none", flexShrink: 0 };
}
