// Drag-to-resize for the app navbar. Mantine's AppShell drives the navbar's width from a Python-
// controlled prop, but a live drag needs pixel-by-pixel visual feedback with no server round trip
// per frame -- this mutates the navbar element's inline style directly while dragging, and only
// reports the final width back to Dash (via `set_props`, Dash 4.4+) once the drag ends, which is
// what persists it to the `navbar-width-store` `dcc.Store` server-side.
(() => {
    var MIN_WIDTH = 260;
    var MAX_WIDTH = 640;

    function clamp(width) {
        return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width));
    }

    document.addEventListener("mousedown", (event) => {
        var handle = event.target.closest("#navbar-resize-handle");
        if (!handle) {
            return;
        }
        var navbar = document.getElementById("navbar");
        if (!navbar) {
            return;
        }
        event.preventDefault();
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";

        function onMove(moveEvent) {
            navbar.style.width = clamp(moveEvent.clientX) + "px";
        }

        function onUp() {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            var width = clamp(parseInt(navbar.style.width, 10) || navbar.getBoundingClientRect().width);
            window.dash_clientside.set_props("navbar-width-store", { data: width });
        }

        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });
})();
