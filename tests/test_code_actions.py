from copy import deepcopy
from LSP.plugin.core.protocol import Point, Range
from LSP.plugin.core.typing import Dict, Generator, List, Tuple
from LSP.plugin.core.url import filename_to_uri
from LSP.plugin.core.views import entire_content
from LSP.plugin.code_actions import run_code_action_or_command
from setup import TextDocumentTestCase
from test_single_document import TEST_FILE_PATH

TEST_FILE_URI = filename_to_uri(TEST_FILE_PATH)


def create_test_code_action(document_version: int, edits: List[Tuple[str, Range]], kind: str = None) -> Dict:
    def edit_to_lsp(edit: Tuple[str, Range]) -> Dict:
        new_text, range = edit
        return {
            "newText": new_text,
            "range": range.to_lsp()
        }
    actions = {
        "title": "Fix errors",
        "edit": {
            "documentChanges": [
                {
                    "textDocument": {
                        "uri": TEST_FILE_URI,
                        "version": document_version
                    },
                    "edits": list(map(edit_to_lsp, edits))
                }
            ]
        }
    }
    if kind:
        actions['kind'] = kind
    return actions


def create_test_diagnostics(diagnostics: List[Tuple[str, Range]]) -> Dict:
    def diagnostic_to_lsp(diagnostic: Tuple[str, Range]) -> Dict:
        message, range = diagnostic
        return {
            "message": message,
            "range": range.to_lsp()
        }
    return {
        "uri": TEST_FILE_URI,
        "diagnostics": list(map(diagnostic_to_lsp, diagnostics))
    }


class CodeActionsTestCase(TextDocumentTestCase):
    def test_applies_code_actions(self) -> Generator:
        self.insert_characters('a\nb')
        yield from self.await_message("textDocument/didChange")
        code_action = create_test_code_action(self.view.change_count(), [
            ("c", Range(Point(0, 0), Point(0, 1))),
            ("d", Range(Point(1, 0), Point(1, 1))),
        ])
        run_code_action_or_command(self.view, self.config.name, code_action)
        self.assertEquals(entire_content(self.view), 'c\nd')

    def test_does_not_apply_with_nonmatching_document_version(self) -> Generator:
        initial_content = 'a\nb'
        self.insert_characters(initial_content)
        yield from self.await_message("textDocument/didChange")
        code_action = create_test_code_action(0, [
            ("c", Range(Point(0, 0), Point(0, 1))),
            ("d", Range(Point(1, 0), Point(1, 1))),
        ])
        run_code_action_or_command(self.view, self.config.name, code_action)
        self.assertEquals(entire_content(self.view), initial_content)


class CodeActionsOnSaveTestCase(TextDocumentTestCase):
    def setUp(self) -> Generator:
        yield from super().setUp()
        self.view.settings().set('lsp_code_actions_on_save', {'source.fixAll': True})

    def doCleanups(self) -> Generator:
        yield from super().doCleanups()
        self.view.settings().set('lsp_code_actions_on_save', {})

    def get_test_server_capabilities(self) -> dict:
        capabilities = deepcopy(super().get_test_server_capabilities())
        capabilities['capabilities']['codeActionProvider'] = {'codeActionKinds': []}
        return capabilities

    def test_applies_matching_kind(self) -> Generator:
        yield from self._setup_document_with_missing_missing()
        code_action_kind = 'source.fixAll'
        code_action = create_test_code_action(
            self.view.change_count(),
            [(';', Range(Point(0, 11), Point(0, 11)))],
            code_action_kind
        )
        self.set_response('textDocument/codeAction', [code_action])
        self.view.run_command('save')
        yield from self.await_message('textDocument/codeAction')
        self.assertEquals(entire_content(self.view), 'const x = 1;')
        yield from self.await_clear_view_and_save()

    def test_no_matching_kind(self) -> Generator:
        yield from self._setup_document_with_missing_missing()
        initial_content = 'const x = 1'
        code_action_kind = 'source.fixOther'
        code_action = create_test_code_action(
            self.view.change_count(),
            [(';', Range(Point(0, 11), Point(0, 11)))],
            code_action_kind
        )
        self.set_response('textDocument/codeAction', [code_action])
        self.view.run_command('save')
        yield from self.await_message('textDocument/codeAction')
        self.assertEquals(entire_content(self.view), initial_content)
        yield from self.await_clear_view_and_save()

    def _setup_document_with_missing_missing(self) -> Generator:
        self.insert_characters('const x = 1')
        yield from self.await_message("textDocument/didChange")
        yield from self.await_client_notification(
            "textDocument/publishDiagnostics",
            create_test_diagnostics([
                ('Missing semicolon', Range(Point(0, 11), Point(0, 11))),
            ])
        )
