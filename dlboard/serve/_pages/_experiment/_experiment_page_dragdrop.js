// Drag-and-drop for panels (`.dl-panel-drag-handle`) and charts (`.dl-chart-drag-handle`): reorder
// within a panel, move a chart to a different panel, or drop either onto a tab (`.dl-tab-target`).
// Only the grip handle is `draggable`, not the whole row, so buttons/accordion controls still work.
// No server round trip mid-drag -- one `set_props` call on drop, consumed in `_panel_controls.py`.
//
// `makePanelDrag`/`makeChartDrag` share one interface (`onDragOver`, `onDrop`, `tabDropPayload`);
// `dragstart`/`dragover`/`drop` dispatch to whichever is active rather than branching on it.
//
// A drop target gets a thin insertion-line indicator (which side the drop lands on) or, for a
// whole-area target (a tab, an empty panel body), a background highlight -- both toggled directly
// here rather than through Dash, so they survive every re-render untouched.
//
// HTML5 drag/drop doesn't auto-scroll, so a target below the fold was unreachable -- `autoScrollTick`
// nudges the page via `window.scrollBy` while the cursor sits near a viewport edge.
(() => {
    let activeDrag = null; // a makePanelDrag/makeChartDrag object, or null when nothing's being dragged
    let pointerY = null; // latest dragover clientY, read by the auto-scroll loop below

    const EDGE_SCROLL_ZONE_PX = 72;
    const MAX_EDGE_SCROLL_PX_PER_FRAME = 18;

    function autoScrollTick() {
        if (!activeDrag) {
            return;
        }
        if (pointerY !== null) {
            const viewportHeight = window.innerHeight;
            if (pointerY < EDGE_SCROLL_ZONE_PX) {
                const strength = 1 - pointerY / EDGE_SCROLL_ZONE_PX;
                window.scrollBy(0, -MAX_EDGE_SCROLL_PX_PER_FRAME * strength);
            } else if (pointerY > viewportHeight - EDGE_SCROLL_ZONE_PX) {
                const strength = 1 - (viewportHeight - pointerY) / EDGE_SCROLL_ZONE_PX;
                window.scrollBy(0, MAX_EDGE_SCROLL_PX_PER_FRAME * strength);
            }
        }
        requestAnimationFrame(autoScrollTick);
    }

    function makeIndicator(className) {
        const el = document.createElement("div");
        el.className = className;
        el.style.display = "none";
        document.body.appendChild(el);
        return el;
    }

    const panelIndicator = makeIndicator("dl-panel-drop-indicator");
    const chartIndicator = makeIndicator("dl-chart-drop-indicator");

    function makeHighlightTracker(className) {
        let current = null;
        return (el) => {
            if (current === el) {
                return;
            }
            if (current) {
                current.classList.remove(className);
            }
            current = el;
            if (current) {
                current.classList.add(className);
            }
        };
    }

    const setHighlightedTab = makeHighlightTracker("dl-tab-drop-target");
    const setHighlightedPanelBody = makeHighlightTracker("dl-panel-body-drop-target");

    function hideIndicators() {
        panelIndicator.style.display = "none";
        chartIndicator.style.display = "none";
        setHighlightedTab(null);
        setHighlightedPanelBody(null);
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

    function makePanelDrag(name) {
        return {
            tabDropPayload(tab) {
                return { store: "tab-drop-request", data: { panel: name, tab: tab } };
            },
            onDragOver(event) {
                const header = event.target.closest(".dl-panel-item-header");
                if (!header) {
                    return;
                }
                event.preventDefault();
                const rect = header.getBoundingClientRect();
                showPanelIndicator(header, event.clientY - rect.top > rect.height / 2);
            },
            onDrop(event) {
                const header = event.target.closest(".dl-panel-item-header");
                if (!header) {
                    return;
                }
                event.preventDefault();
                const targetHandle = header.querySelector(".dl-panel-drag-handle");
                const targetName = targetHandle ? targetHandle.getAttribute("data-panel-name") : null;
                if (!targetName || targetName === name) {
                    return;
                }
                const rect = header.getBoundingClientRect();
                const after = event.clientY - rect.top > rect.height / 2;
                window.dash_clientside.set_props("panel-reorder-request", {
                    data: { panel: name, target: targetName, after: after },
                });
            },
        };
    }

    function makeChartDrag(panel, index) {
        return {
            tabDropPayload(tab) {
                return { store: "chart-tab-drop-request", data: { panel: panel, index: index, tab: tab } };
            },
            onDragOver(event) {
                const item = event.target.closest(".dl-chart-item");
                if (item) {
                    event.preventDefault();
                    setHighlightedPanelBody(null);
                    const rect = item.getBoundingClientRect();
                    showChartIndicator(item, event.clientX - rect.left > rect.width / 2);
                    return;
                }
                // A different panel's body appends rather than inserting next to a chart; your own panel isn't a target.
                const body = event.target.closest(".dl-panel-body");
                if (body && body.getAttribute("data-panel-name") !== panel) {
                    event.preventDefault();
                    chartIndicator.style.display = "none";
                    setHighlightedPanelBody(body);
                    return;
                }
                setHighlightedPanelBody(null);
            },
            onDrop(event) {
                const item = event.target.closest(".dl-chart-item");
                if (item) {
                    const chartHandle = item.querySelector(".dl-chart-drag-handle");
                    const targetPanel = chartHandle ? chartHandle.getAttribute("data-panel-name") : null;
                    const targetIndex = chartHandle
                        ? parseInt(chartHandle.getAttribute("data-chart-index"), 10)
                        : null;
                    if (targetPanel === null || targetIndex === null) {
                        return;
                    }
                    if (targetPanel === panel && targetIndex === index) {
                        return;
                    }
                    event.preventDefault();
                    const rect = item.getBoundingClientRect();
                    const after = event.clientX - rect.left > rect.width / 2;
                    if (targetPanel === panel) {
                        window.dash_clientside.set_props("chart-reorder-request", {
                            data: { panel: panel, index: index, target_index: targetIndex, after: after },
                        });
                    } else {
                        window.dash_clientside.set_props("chart-panel-move-request", {
                            data: {
                                panel: panel,
                                index: index,
                                target_panel: targetPanel,
                                target_index: targetIndex,
                                after: after,
                            },
                        });
                    }
                    return;
                }
                const body = event.target.closest(".dl-panel-body");
                const targetPanel = body ? body.getAttribute("data-panel-name") : null;
                if (!targetPanel || targetPanel === panel) {
                    return;
                }
                event.preventDefault();
                window.dash_clientside.set_props("chart-panel-move-request", {
                    data: {
                        panel: panel,
                        index: index,
                        target_panel: targetPanel,
                        target_index: null,
                        after: true,
                    },
                });
            },
        };
    }

    document.addEventListener("dragstart", (event) => {
        const panelHandle = event.target.closest(".dl-panel-drag-handle");
        if (panelHandle) {
            const header = panelHandle.closest(".dl-panel-item-header");
            activeDrag = makePanelDrag(panelHandle.getAttribute("data-panel-name"));
            event.dataTransfer.effectAllowed = "move";
            if (header) {
                event.dataTransfer.setDragImage(header, 10, 10);
            }
            requestAnimationFrame(autoScrollTick);
            return;
        }
        const chartHandle = event.target.closest(".dl-chart-drag-handle");
        if (chartHandle) {
            const item = chartHandle.closest(".dl-chart-item");
            activeDrag = makeChartDrag(
                chartHandle.getAttribute("data-panel-name"),
                parseInt(chartHandle.getAttribute("data-chart-index"), 10),
            );
            event.dataTransfer.effectAllowed = "move";
            if (item) {
                event.dataTransfer.setDragImage(item, 10, 10);
            }
            requestAnimationFrame(autoScrollTick);
        }
    });

    document.addEventListener("dragover", (event) => {
        pointerY = event.clientY;
        if (!activeDrag) {
            return;
        }
        const tabTarget = event.target.closest(".dl-tab-target");
        if (tabTarget) {
            event.preventDefault();
            panelIndicator.style.display = "none";
            chartIndicator.style.display = "none";
            setHighlightedPanelBody(null);
            setHighlightedTab(tabTarget);
            return;
        }
        setHighlightedTab(null);
        activeDrag.onDragOver(event);
    });

    document.addEventListener("drop", (event) => {
        if (activeDrag) {
            const tabTarget = event.target.closest(".dl-tab-target");
            if (tabTarget) {
                event.preventDefault();
                const request = activeDrag.tabDropPayload(tabTarget.getAttribute("data-tab"));
                window.dash_clientside.set_props(request.store, { data: request.data });
            } else {
                activeDrag.onDrop(event);
            }
        }
        activeDrag = null;
        pointerY = null;
        hideIndicators();
    });

    document.addEventListener("dragend", () => {
        activeDrag = null;
        pointerY = null;
        hideIndicators();
    });
})();
