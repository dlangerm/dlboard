// What someone typed in an experiment picker, for the server to search (`_experiment_picker.py`); nothing else.
// A picker changes its own search as it mounts (a Select shows its value's label there), and one drawn by a callback,
// as the project page's are, would otherwise ask the server on every page load for a search nobody made.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function experimentSearch(search, picked, options, asked) {
    const noUpdate = window.dash_clientside.no_update;
    const typed = search ?? "";
    const label = (options ?? []).find((option) => option.value === picked)?.label;
    if (typed === label) {
        return noUpdate;
    }
    // Clearing what was typed lists the newest again; a search empty from the start asks for nothing.
    if (typed === "") {
        return asked ? "" : noUpdate;
    }
    return typed === asked ? noUpdate : typed;
}
