import sublime

from ..color import remove_color_boxes
from ..diagnostics import DiagnosticsPresenter
from ..highlights import remove_highlights
from .logging import set_debug_logging, set_exception_logging
from .panels import destroy_output_panels, ensure_panel, PanelName
from .popups import popups
from .registry import windows, unload_sessions
from .sessions import AbstractPlugin
from .sessions import register_plugin
from .settings import settings, load_settings, unload_settings
from .typing import Optional, List, Type


def ensure_server_panel(window: sublime.Window) -> Optional[sublime.View]:
    return ensure_panel(window, PanelName.LanguageServers, "", "", "Packages/LSP/Syntaxes/ServerLog.sublime-syntax")


def get_final_subclasses(derived: List[Type], results: List[Type]) -> None:
    for d in derived:
        d_subclasses = d.__subclasses__()
        if len(d_subclasses) > 0:
            get_final_subclasses(d_subclasses, results)
        else:
            results.append(d)


def startup() -> None:
    load_settings()
    popups.load_css()
    set_debug_logging(settings.log_debug)
    set_exception_logging(True)
    final_subclasses = []  # type: List[Type[AbstractPlugin]]
    get_final_subclasses(AbstractPlugin.__subclasses__(), final_subclasses)
    for plugin_class in final_subclasses:
        # Unfortunately we need to do this ourselves instead of helper packages registering themselves in plugin_loaded
        # and unregistering themselves in plugin_unloaded. This is because this base LSP package is loaded before
        # the helper packages. If the helper packages were loaded before this package, we would not have to do this.
        # Perhaps we can come up with a scheme to load the helper packages before this package?
        register_plugin(plugin_class)
    windows.set_diagnostics_ui(DiagnosticsPresenter)
    windows.set_server_panel_factory(ensure_server_panel)
    windows.set_settings_factory(settings)
    sublime.status_message("LSP initialized")
    start_active_window()


def shutdown() -> None:
    # Also needs to handle package being disabled or removed
    # https://github.com/sublimelsp/LSP/issues/375
    unload_settings()

    for window in sublime.windows():
        unload_sessions(window)  # unloads view state from document sync and diagnostics
        destroy_output_panels(window)  # references and diagnostics panels

        for view in window.views():
            if view.file_name():
                remove_highlights(view)
                remove_color_boxes(view)


def start_active_window() -> None:
    window = sublime.active_window()
    if window:
        windows.lookup(window).start_active_views()
