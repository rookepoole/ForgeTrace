from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VerifiedFolderImportSurfaceTest(unittest.TestCase):
    def test_folder_input_is_retained_verified_retried_and_expanded(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        required = (
            "const files=Array.from(input.files||[])",
            "input.disabled=true",
            "await uploadSelection(selection)",
            "finally{input.value='';input.disabled=false;}",
            "verifyRepositoryImport(repositoryId,filePaths,folderPaths=[])",
            "automatic verification retry",
            "rememberExpandedImport(repositoryId",
            "Import verified: all",
            'id="expandAllBtn"',
            'id="collapseAllBtn"',
            'id="folderImportReport"',
        )
        for marker in required:
            self.assertIn(marker, html)

        handler_start = html.index("$('#folderInput').addEventListener('change',async event=>")
        handler_end = html.index("$('#uploadFilesBtn').addEventListener", handler_start)
        handler = html[handler_start:handler_end]
        self.assertLess(handler.index("await uploadSelection(selection)"), handler.index("input.value=''"))

    def test_one_launch_waits_for_successful_server_bind(self) -> None:
        app = (ROOT / "forgetrace" / "app.py").read_text(encoding="utf-8")
        batch = (ROOT / "START_FORGETRACE.bat").read_text(encoding="utf-8")
        shell = (ROOT / "START_FORGETRACE.sh").read_text(encoding="utf-8")
        self.assertIn("server = create_server(app, host, port, surface=surface)", app)
        self.assertIn("if open_browser and surface == \"owner\"", app)
        self.assertLess(app.index("server = create_server"), app.index("if open_browser and surface"))
        self.assertIn("--open-browser", batch)
        self.assertIn("--open-browser", shell)
        self.assertNotIn("Start-Sleep", batch)
        self.assertNotIn("xdg-open", shell)


if __name__ == "__main__":
    unittest.main(verbosity=2)
