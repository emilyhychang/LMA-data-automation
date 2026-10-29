import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


class HistoryUITest(unittest.TestCase):
    def test_delete_callback_only_updates_history_and_preserves_import_result(self):
        source = (Path(__file__).resolve().parents[1] / 'app.py').read_text()
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'render_file_history')
        self.assertEqual(ast.unparse(function.decorator_list[0]), 'st.fragment')
        function.decorator_list = []
        st = MagicMock()
        result = object()
        st.session_state = {'import_result': result}
        st.columns.return_value = (MagicMock(), MagicMock())
        callbacks = []
        def capture_button(label, **kwargs):
            if label == '×':
                callbacks.append((kwargs['on_click'], kwargs['args']))
        st.columns.return_value[1].button.side_effect = capture_button
        store = MagicMock()
        store.remove_import_history_entry.return_value = True
        entry = SimpleNamespace(record_id=1, dataset_version=2,
                                source_name='wrong.csv', imported_by='admin',
                                imported_at=MagicMock(), cleaned_csv=b'a', analysis_csv=b'b')
        history = [entry]
        namespace = {'st': st, 'store': store, 'is_admin': True, 'Path': Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<history>', 'exec'), namespace)
        namespace['render_file_history'](history)
        callback, args = callbacks[0]
        callback(*args)
        self.assertEqual(history, [])
        self.assertIs(st.session_state['import_result'], result)
        store.remove_import_history_entry.assert_called_once_with(entry)
        store.import_history.assert_not_called()
        store.downloads.assert_not_called()
        st.rerun.assert_not_called()

    def test_failed_delete_keeps_history_visible(self):
        source = (Path(__file__).resolve().parents[1] / 'app.py').read_text()
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'render_file_history')
        function.decorator_list = []
        st = MagicMock()
        st.session_state = {}
        st.columns.return_value = (MagicMock(), MagicMock())
        callbacks = []

        def capture_button(label, **kwargs):
            if label == '×':
                callbacks.append((kwargs['on_click'], kwargs['args']))

        st.columns.return_value[1].button.side_effect = capture_button
        store = MagicMock()
        store.remove_import_history_entry.return_value = False
        entry = SimpleNamespace(record_id=1, dataset_version=2,
                                source_name='wrong.csv', imported_by='admin',
                                imported_at=MagicMock(), cleaned_csv=b'a', analysis_csv=b'b')
        history = [entry]
        namespace = {'st': st, 'store': store, 'is_admin': True, 'Path': Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<history>', 'exec'), namespace)

        namespace['render_file_history'](history)
        callback, args = callbacks[0]
        callback(*args)

        self.assertEqual(history, [entry])
        self.assertIn('history_delete_error', st.session_state)
        st.rerun.assert_not_called()
