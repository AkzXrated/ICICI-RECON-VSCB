import unittest
import os
import re
import subprocess
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "templates", "index.html")

class TestFrontendSyntax(unittest.TestCase):
    """Ensure templates/index.html JavaScript parses without syntax errors and defines key functions."""

    def test_01_inline_script_valid_syntax(self):
        self.assertTrue(os.path.exists(INDEX_HTML), f"File {INDEX_HTML} not found")
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        scripts = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)
        self.assertGreater(len(scripts), 0, "No inline <script> found in index.html")

        # Write each script to a temp file and run node --check
        for idx, script in enumerate(scripts):
            temp_path = os.path.join(BASE_DIR, f"_test_syntax_{idx}.js")
            try:
                with open(temp_path, 'w', encoding='utf-8') as tf:
                    tf.write(script)
                
                res = subprocess.run(['node', '--check', temp_path], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Syntax error in script {idx}:\n{res.stderr}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    def test_02_key_functions_declared(self):
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        essential_functions = [
            'selectFy',
            'fetchFys',
            'checkCurrentFy',
            'render',
            'toggleSelect',
            'commitMatch',
            'autoSelect',
            'editStickyNote',
            'saveStickyNote',
            'openBatchNoteModal',
            'submitBatchNotes',
            'toggleSidebar',
            'renderNotesHub',
            'updateFooterHelpers'
        ]

        for fn in essential_functions:
            pattern = rf'function\s+{fn}\b|window\.{fn}\s*='
            self.assertTrue(
                re.search(pattern, content),
                f"Required function '{fn}' was not declared in templates/index.html"
            )

if __name__ == '__main__':
    unittest.main()
