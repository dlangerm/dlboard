// Drag-and-drop reordering for panels (`.dl-panel-drag-handle`, dragging `.dl-panel-item-header`
// rows) and charts within a panel (`.dl-chart-drag-handle`, dragging `.dl-chart-item` boxes).
// Only the small grip handle is `draggable` -- not the whole row -- so grabbing a button or the
// accordion control still works normally; the handle sets the drag image to its whole row via
// `setDragImage`. Mirrors `navbar_resize.js`: no server round trip during the drag itself, only a
// single `set_props` call on drop, consumed by a callback in `_panel_controls.py`.
//
// While dragging, a thin insertion-line indicator (not an outline around the whole target -- that
// doesn't say which *side* of it the drop lands on) tracks the cursor: a horizontal line between
// two panel rows, a vertical line between two chart items. Both indicators are plain `fixed`-
// positioned elements owned entirely by this script (created once, appended to `<body>`), not part
// of Dash's render tree, so they survive every panel/chart re-render untouched.
(() => {
    let draggingPanel = null; // panel name
    let draggingChart = null; // {panel, index}

    function makeIndicator(className) {
        const el = document.createElement("div");
        el.className = className;
        el.style.display = "none";
        document.body.appendChild(el);
        return el;
    }

    const panelIndicator = makeIndicator("dl-panel-drop-indicator");
    const chartIndicator = makeIndicator("dl-chart-drop-indicator");

    function hideIndicators() {
        panelIndicator.style.display = "none";
        chartIndicator.style.display = "none";
    }

    function showPanelIndicator(header, after) {
        const rect = header.getBoundingClientRect();
        panelIndicator.style.display = "block";
        panelIndicator.style.left = `${rect.left}px`;
        panelIndicator.style.width = `${rect.width}px`;
        panelIndicator.style.top = `${(after ? rect.bottom : rect.top) - 1}px`;
    }

    function showChartIndicator(item, after) {
        const rect = item.getBoundingClientRect();
        chartIndicator.style.display = "block";
        chartIndicator.style.top = `${rect.top}px`;
        chartIndicator.style.height = `${rect.height}px`;
        chartIndicator.style.left = `${(after ? rect.right : rect.left) - 1}px`;
    }

    document.addEventListener("dragstart", (event) => {
        const panelHandle = event.target.closest(".dl-panel-drag-handle");
        if (panelHandle) {
            const header = panelHandle.closest(".dl-panel-item-header");
            draggingPanel = panelHandle.getAttribute("data-panel-name");
            event.dataTransfer.effectAllowed = "move";
            if (header) {
                event.dataTransfer.setDragImage(header, 10, 10);
            }
            return;
        }
        const chartHandle = event.target.closest(".dl-chart-drag-handle");
        if (chartHandle) {
            const item = chartHandle.closest(".dl-chart-item");
            draggingChart = {
                panel: chartHandle.getAttribute("data-panel-name"),
                index: parseInt(chartHandle.getAttribute("data-chart-index"), 10),
            };
            event.dataTransfer.effectAllowed = "move";
            if (item) {
                event.dataTransfer.setDragImage(item, 10, 10);
            }
        }
    });

    document.addEventListener("dragover", (event) => {
        if (draggingPanel) {
            const header = event.target.closest(".dl-panel-item-header");
            if (header) {
                event.preventDefault();
                const rect = header.getBoundingClientRect();
                showPanelIndicator(header, event.clientY - rect.top > rect.height / 2);
            }
        } else if (draggingChart) {
            const item = event.target.closest(".dl-chart-item");
            if (item) {
                event.preventDefault();
                const rect = item.getBoundingClientRect();
                showChartIndicator(item, event.clientX - rect.left > rect.width / 2);
            }
        }
    });

    function dropPanel(event) {
        const header = event.target.closest(".dl-panel-item-header");
        if (!header) {
            return;
        }
        event.preventDefault();
        const targetHandle = header.querySelector(".dl-panel-drag-handle");
        const targetName = targetHandle ? targetHandle.getAttribute("data-panel-name") : null;
        if (!targetName || targetName === draggingPanel) {
            return;
        }
        const rect = header.getBoundingClientRect();
        const after = event.clientY - rect.top > rect.height / 2;
        window.dash_clientside.set_props("panel-reorder-request", {
            data: { panel: draggingPanel, target: targetName, after: after },
        });
    }

    function dropChart(event) {
        const item = event.target.closest(".dl-chart-item");
        if (!item) {
            return;
        }
        event.preventDefault();
        const chartHandle = item.querySelector(".dl-chart-drag-handle");
        const targetPanel = chartHandle ? chartHandle.getAttribute("data-panel-name") : null;
        const targetIndex = chartHandle ? parseInt(chartHandle.getAttribute("data-chart-index"), 10) : null;
        if (
            targetPanel !== draggingChart.panel ||
            targetIndex === null ||
            targetIndex === draggingChart.index
        ) {
            return;
        }
        const rect = item.getBoundingClientRect();
        const after = event.clientX - rect.left > rect.width / 2;
        window.dash_clientside.set_props("chart-reorder-request", {
            data: {
                panel: draggingChart.panel,
                index: draggingChart.index,
                target_index: targetIndex,
                after: after,
            },
        });
    }

    document.addEventListener("drop", (event) => {
        if (draggingPanel) {
            dropPanel(event);
        } else if (draggingChart) {
            dropChart(event);
        }
        draggingPanel = null;
        draggingChart = null;
        hideIndicators();
    });

    document.addEventListener("dragend", () => {
        draggingPanel = null;
        draggingChart = null;
        hideIndicators();
    });
})();
