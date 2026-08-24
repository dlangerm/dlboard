"""Constants for use by plugins and within the app."""

from typing import Final

PAGE_HOME_ID: Final = "homepage-container"
PAGE_ADMIN_ID: Final = "admin-container"
PAGE_PROJECT_ID: Final = "project-container"
PAGE_EXPERIMENT_ID: Final = "experiment-container"
EXPERIMENT_HEADER_ID: Final = "experiment-header"
EXPERIMENT_HEADER_ACTIONS_ID: Final = "experiment-header-actions"

STATE_PROJECT_ID: Final = "project-id-state"
STATE_EXPERIMENT_ID: Final = "experiment-id-state"
STATE_HPARAMS: Final = "hparams-state"
STATE_PAGE_STORAGE: Final = "current-page"

MANTINE_PROVIDER_ID: Final = "mantine-provider"
NAVBAR_ID: Final = "navbar"
NAVBAR_CONTENT_ID = "navbar-content"
NAVBAR_COLLAPSED_STORE_ID = "navbar-collapsed-store"
NAVBAR_COLLAPSE_TOGGLE_ID = "navbar-collapse-toggle"
HEADER_USER_INDICATOR_ID: Final = "header-user-indicator"

HPARAM_DRAWER_ID = "hparam-drawer"
HPARAM_DRAWER_TOGGLE_ID = "hparam-drawer-toggle"
HPARAM_TABLE_BODY_ID = "hparam-table-body"
HPARAM_TABLE_ID: Final = "hparam-table"
HPARAM_COL_SELECT_ID: Final = "hparam-col-select"
HPARAM_DATATABLE_ID = "hparam-datatable"

LOCATION_ID: Final = "location"
METRIC_CONTENT_ID: Final = "metrics-view"
PAGE_BREADCRUMB_ID: Final = "page-breadcrumb"

EXCLUDED_RUNS_KEY: Final = "excluded_runs"
SELECTED_HPARAM_COLS_KEY: Final = "hparam-table-selected"

DELETE_PROJECT_BUTTON_ID: Final = "delete-project-button"
DELETE_PROJECT_MODAL_ID: Final = "delete-project-modal"
DELETE_PROJECT_CONFIRM_ID: Final = "delete-project-confirm"
DELETE_PROJECT_CANCEL_ID: Final = "delete-project-cancel"

DELETE_EXPERIMENT_BUTTON_ID: Final = "delete-experiment-button"
DELETE_EXPERIMENT_MODAL_ID: Final = "delete-experiment-modal"
DELETE_EXPERIMENT_CONFIRM_ID: Final = "delete-experiment-confirm"
DELETE_EXPERIMENT_CANCEL_ID: Final = "delete-experiment-cancel"

DELETE_RUN_SELECT_ID: Final = "delete-run-select"
DELETE_RUN_BUTTON_ID: Final = "delete-run-button"
DELETE_RUN_MODAL_ID: Final = "delete-run-modal"
DELETE_RUN_CONFIRM_ID: Final = "delete-run-confirm"
DELETE_RUN_CANCEL_ID: Final = "delete-run-cancel"

ADMIN_TABS_ID: Final = "admin-tabs"
ADMIN_TRASH_CONTENT_ID: Final = "admin-trash-content"
ADMIN_AUDIT_LOG_CONTENT_ID: Final = "admin-audit-log-content"
ADMIN_ABOUT_CONTENT_ID: Final = "admin-about-content"
ADMIN_RESTORE_BUTTON_TYPE: Final = "admin-restore-button"
ADMIN_PURGE_BUTTON_TYPE: Final = "admin-purge-button"
ADMIN_PURGE_MODAL_ID: Final = "admin-purge-modal"
ADMIN_PURGE_CONFIRM_ID: Final = "admin-purge-confirm"
ADMIN_PURGE_CANCEL_ID: Final = "admin-purge-cancel"
ADMIN_PENDING_PURGE_STORE_ID: Final = "admin-pending-purge-store"
ADMIN_RESUME_PURGE_ID: Final = "admin-resume-purge"
