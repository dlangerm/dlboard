// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function imageSeriesPage(page, ids) {
    return ids.map((id) => (id.page === page - 1 ? {} : { display: "none" }));
}
