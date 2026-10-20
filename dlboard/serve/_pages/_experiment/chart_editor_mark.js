// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartEditorMark(target, _draftIds) {
    // Outlines the card of the chart the editor is open on, from the moment it opens: the card shows the saved chart
    // until the form makes it a different one, so nothing else says which chart the form is for. Cards drawn again
    // (a panel opened, a page of charts turned) are marked again, which is why this also listens for them.
    const editing = target?.kind === "edit" ? target.charts[0].chart_id : "";
    for (const card of document.querySelectorAll(".dl-chart-item[data-dl-editing]")) {
        card.removeAttribute("data-dl-editing");
    }
    if (editing) {
        const card = [...document.querySelectorAll(".dl-chart-item[data-chart-id]")].find(
            (item) => item.dataset.chartId === editing,
        );
        card?.setAttribute("data-dl-editing", "");
    }
    return editing;
}
