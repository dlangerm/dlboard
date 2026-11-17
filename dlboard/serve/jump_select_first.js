// Clientside body for `_jump.py`'s results: Mantine takes a search's first option as the search changes, but the server's
// results for it arrive after that keystroke, with none taken. Take the first of them as they arrive, so Enter goes to it.
// They arrive before React has drawn them, and how many frames that takes varies: look each frame until they are drawn
// (for about a second at most), rather than once, which left nothing taken whenever the drawing took longer (#661).
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function jumpSelectFirst(data, inputId) {
    if (!data?.length) {
        return;
    }
    const maxFrames = 60;
    let frames = 0;
    const take = () => {
        const input = document.getElementById(inputId);
        if (!input || document.activeElement !== input) {
            return; // closed, or the person has moved on: theirs to choose
        }
        const options = document.getElementById(input.getAttribute("aria-controls") ?? "");
        if (!options?.querySelector("[data-combobox-option]")) {
            frames += 1;
            if (frames < maxFrames) {
                requestAnimationFrame(take);
            }
            return;
        }
        if (!options.querySelector("[data-combobox-selected]")) {
            // Nothing taken: the combobox's own ArrowDown takes the first option, as it does for someone pressing it.
            input.dispatchEvent(
                new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", bubbles: true }),
            );
        }
    };
    requestAnimationFrame(take);
}
