import sublime
import sublime_plugin
from .core.edit import parse_workspace_edit
from .core.protocol import Diagnostic
from .core.protocol import Request, Point
from .core.registry import LspTextCommand, LSPViewEventListener
from .core.registry import sessions_for_view, client_from_session
from .core.settings import settings
from .core.typing import Any, List, Dict, Callable, Optional, Union, Tuple, Mapping, TypedDict
from .core.url import filename_to_uri
from .core.views import entire_content_range, region_to_range
from .diagnostics import filter_by_point, view_diagnostics

CodeActionOrCommand = TypedDict('CodeActionOrCommand', {
    'title': str,
    'command': Union[dict, str],
    'edit': dict,
    'kind': Optional[str]
}, total=False)
CodeActionsResponse = Optional[List[CodeActionOrCommand]]
CodeActionsByConfigName = Dict[str, List[CodeActionOrCommand]]


class CodeActionsCollector(object):
    def __init__(self, on_complete_handler: Callable[[CodeActionsByConfigName], None],
                 only_kinds: List[str]):
        self._on_complete_handler = on_complete_handler
        self._only_kinds = only_kinds
        self._commands_by_config = {}  # type: CodeActionsByConfigName

    def create_collector(self, config_name: str) -> Callable[[CodeActionsResponse], None]:
        return lambda actions: self._collect(config_name, actions)

    def _collect(self, config_name: str, actions: CodeActionsResponse) -> None:
        self._commands_by_config[config_name] = self._filter_by_kind(actions or [])

    def _filter_by_kind(self, actions: List[CodeActionOrCommand]) -> List[CodeActionOrCommand]:
        print('FILTERING', actions, self._only_kinds)
        if self._only_kinds:
            return [action for action in actions if self._matches_kind(self._only_kinds, action.get('kind'))]
        else:
            return actions

    def _matches_kind(self, requested_kinds: List[str], got_kind: Optional[str]) -> bool:
        return bool(got_kind and got_kind in requested_kinds)

    def get_actions(self) -> CodeActionsByConfigName:
        return self._commands_by_config

    def deliver(self) -> None:
        self._on_complete_handler(self._commands_by_config)


class CodeActionsAsyncCollector(CodeActionsCollector):
    def __init__(self, on_complete_handler: Callable[[CodeActionsByConfigName], None],
                 only_kinds: List[str]):
        super().__init__(on_complete_handler, only_kinds)
        self._requested_configs = []  # type: List[str]

    def create_collector(self, config_name: str) -> Callable[[CodeActionsResponse], None]:
        self._requested_configs.append(config_name)
        return super().create_collector(config_name)

    def _collect(self, config_name: str, actions: CodeActionsResponse) -> None:
        super()._collect(config_name, actions)
        if len(self._requested_configs) == len(self._commands_by_config):
            self.deliver()


class CodeActionsManager(object):
    """ Collects and caches code actions"""

    def __init__(self) -> None:
        self._requests = {}  # type: Dict[str, CodeActionsCollector]

    def request(self, view: sublime.View, point: int,
                actions_handler: Callable[[CodeActionsByConfigName], None]) -> None:
        current_location = self.get_location_key(view, point)
        # debug("requesting actions for {}".format(current_location))
        if current_location in self._requests:
            actions_handler(self._requests[current_location].get_actions())
        else:
            self._requests.clear()
            self._requests[current_location] = request_code_actions(view, point, actions_handler)

    def get_location_key(self, view: sublime.View, point: int) -> str:
        return "{}#{}:{}".format(view.file_name(), view.change_count(), point)


actions_manager = CodeActionsManager()


def request_code_actions(view: sublime.View, point: Optional[int],
                         actions_handler: Callable[[CodeActionsByConfigName], None],
                         only_kind: List[str] = [],
                         blocking: bool = False) -> CodeActionsCollector:
    print('request_code_actions REQUESTING')
    if blocking:
        return _request_code_actions_without_diagnostics(view, actions_handler, only_kind, blocking)

    diagnostics_by_config = view_diagnostics(view)
    if point is not None:
        diagnostics_by_config = filter_by_point(diagnostics_by_config, Point(*view.rowcol(point)))
    print('request_code_actions REQUESTED', diagnostics_by_config)
    return _request_code_actions_with_diagnostics(
        view, diagnostics_by_config, point, actions_handler, only_kind, blocking)


def _request_code_actions_with_diagnostics(view: sublime.View, diagnostics_by_config: Dict[str, List[Diagnostic]],
                                           point: Optional[int],
                                           actions_handler: Callable[[CodeActionsByConfigName], None],
                                           only_kind: List[str],
                                           blocking: Optional[bool] = False) -> CodeActionsCollector:
    CodeActionsCollectorType = CodeActionsCollector if blocking else CodeActionsAsyncCollector
    actions_collector = CodeActionsCollectorType(actions_handler, only_kind)

    for session in sessions_for_view(view, point):
        file_name = view.file_name()
        if not file_name or not session.client or not session.has_capability('codeActionProvider'):
            continue

        if session.config.name in diagnostics_by_config:
            diagnostics = diagnostics_by_config[session.config.name]
            if point is None:  # whole document
                relevant_range = entire_content_range(view)
            else:
                relevant_range = diagnostics[0].range if diagnostics else region_to_range(view, view.sel()[0])
            params = {
                "textDocument": {
                    "uri": filename_to_uri(file_name)
                },
                "range": relevant_range.to_lsp(),
                "context": {
                    "diagnostics": list(diagnostic.to_lsp() for diagnostic in diagnostics)
                }
            }
            if only_kind:
                params['context']['only'] = only_kind

            if blocking:
                session.client.execute_request(
                    Request.codeAction(params), actions_collector.create_collector(session.config.name))
            else:
                session.client.send_request(
                    Request.codeAction(params), actions_collector.create_collector(session.config.name))

    if blocking:
        actions_collector.deliver()

    return actions_collector


def _request_code_actions_without_diagnostics(view: sublime.View,
                                              actions_handler: Callable[[CodeActionsByConfigName], None],
                                              only_kind: List[str],
                                              blocking: Optional[bool] = False) -> CodeActionsCollector:
    CodeActionsCollectorType = CodeActionsCollector if blocking else CodeActionsAsyncCollector
    actions_collector = CodeActionsCollectorType(actions_handler, only_kind)

    for session in sessions_for_view(view):
        file_name = view.file_name()
        if not file_name or not session.client or not session.has_capability('codeActionProvider'):
            continue

        params = {
            "textDocument": {
                "uri": filename_to_uri(file_name)
            },
            "range": entire_content_range(view).to_lsp(),
            "context": {
                "diagnostics": []
            }
        }
        if only_kind:
            params['context']['only'] = only_kind

        if blocking:
            session.client.execute_request(
                Request.codeAction(params), actions_collector.create_collector(session.config.name))
        else:
            session.client.send_request(
                Request.codeAction(params), actions_collector.create_collector(session.config.name))

    if blocking:
        actions_collector.deliver()

    return actions_collector


class LspCodeActionsOnSaveListener(LSPViewEventListener):
    @classmethod
    def is_applicable(cls, view_settings: dict) -> bool:
        return len(cls.get_enabled_code_actions_on_save(view_settings)) > 0

    @classmethod
    def get_enabled_code_actions_on_save(self, view_settings: Union[dict, sublime.Settings]) -> List[str]:
        view_code_actions = view_settings.get('lsp_code_actions_on_save') or {}
        code_actions = settings.lsp_code_actions_on_save.copy()
        code_actions.update(view_code_actions)
        return [action for action, enabled in code_actions.items() if enabled]

    def on_pre_save(self) -> None:
        if self.view.file_name():
            print('ON_PRE_SAVE')
            self.trigger_code_actions()
            print('DID_SAVE')

    def trigger_code_actions(self) -> None:
        code_actions = self.get_enabled_code_actions_on_save(self.view.settings())
        if code_actions:
            print('trigger_code_actions - TRYING TO PURGE')
            self.manager.documents.purge_changes(self.view)
            request_code_actions(self.view, None, lambda response: self.handle_response(response),
                                 only_kind=code_actions, blocking=True)

    def handle_response(self, responses: CodeActionsByConfigName) -> None:
        for config_name, code_actions in responses.items():
            for code_action in code_actions:
                run_code_action_or_command(self.view, config_name, code_action)


class LspCodeActionBulbListener(LSPViewEventListener):
    def __init__(self, view: sublime.View) -> None:
        super().__init__(view)
        self._stored_region = sublime.Region(-1, -1)
        self._actions = []  # type: List[CodeActionOrCommand]

    @classmethod
    def is_applicable(cls, _settings: dict) -> bool:
        return settings.show_code_actions_bulb

    def on_selection_modified_async(self) -> None:
        self.hide_bulb()
        self.schedule_request()

    def schedule_request(self) -> None:
        current_region = self.view.sel()[0]
        if self._stored_region != current_region:
            self._stored_region = current_region
            sublime.set_timeout_async(lambda: self.fire_request(current_region), 800)

    def fire_request(self, current_region: sublime.Region) -> None:
        if current_region == self._stored_region:
            self._actions = []
            actions_manager.request(self.view, current_region.begin(), self.handle_responses)

    def handle_responses(self, responses: CodeActionsByConfigName) -> None:
        for _, items in responses.items():
            self._actions.extend(items)
        if len(self._actions) > 0:
            self.show_bulb()

    def show_bulb(self) -> None:
        region = self.view.sel()[0]
        flags = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
        self.view.add_regions('lsp_bulb', [region], 'markup.changed', 'Packages/LSP/icons/lightbulb.png', flags)

    def hide_bulb(self) -> None:
        self.view.erase_regions('lsp_bulb')


def is_command(command_or_code_action: CodeActionOrCommand) -> bool:
    command_field = command_or_code_action.get('command')
    return isinstance(command_field, str)


def execute_server_command(view: sublime.View, config_name: str, command: Mapping[str, Any]) -> None:
    session = next((session for session in sessions_for_view(view) if session.config.name == config_name), None)
    client = client_from_session(session)
    if client:
        client.send_request(
            Request.executeCommand(command),
            handle_command_response)


def handle_command_response(response: 'None') -> None:
    pass


def run_code_action_or_command(view: sublime.View, config_name: str,
                               command_or_code_action: CodeActionOrCommand) -> None:
    if is_command(command_or_code_action):
        execute_server_command(view, config_name, command_or_code_action)
    else:
        # CodeAction can have an edit and/or command.
        maybe_edit = command_or_code_action.get('edit')
        if maybe_edit:
            changes = parse_workspace_edit(maybe_edit)
            window = view.window()
            if window:
                window.run_command("lsp_apply_workspace_edit", {'changes': changes})
        maybe_command = command_or_code_action.get('command')
        if isinstance(maybe_command, dict):
            execute_server_command(view, config_name, maybe_command)


class LspCodeActionsCommand(LspTextCommand):
    def is_enabled(self) -> bool:
        return self.has_client_with_capability('codeActionProvider')

    def run(self, edit: sublime.Edit) -> None:
        self.commands = []  # type: List[Tuple[str, str, CodeActionOrCommand]]
        self.commands_by_config = {}  # type: CodeActionsByConfigName
        actions_manager.request(self.view, self.view.sel()[0].begin(), self.handle_responses)

    def combine_commands(self) -> 'List[Tuple[str, str, CodeActionOrCommand]]':
        results = []
        for config, commands in self.commands_by_config.items():
            for command in commands:
                results.append((config, command['title'], command))
        return results

    def handle_responses(self, responses: CodeActionsByConfigName) -> None:
        self.commands_by_config = responses
        self.commands = self.combine_commands()
        self.show_popup_menu()

    def show_popup_menu(self) -> None:
        if len(self.commands) > 0:
            self.view.show_popup_menu([command[1] for command in self.commands], self.handle_select)
        else:
            self.view.show_popup('No actions available', sublime.HIDE_ON_MOUSE_MOVE_AWAY)

    def handle_select(self, index: int) -> None:
        if index > -1:
            selected = self.commands[index]
            run_code_action_or_command(self.view, selected[0], selected[2])
