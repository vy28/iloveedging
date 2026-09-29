"""Offline checks for the installable terminal-only distribution."""

import io
import json
import unittest
from contextlib import redirect_stderr
from importlib.resources import files
from unittest.mock import Mock, patch

from iloveedging.__main__ import main
from iloveedging.config import Settings
from iloveedging.server import make_handler


class DistributionTests(unittest.TestCase):
    def test_package_includes_styles_and_sample(self):
        package = files("iloveedging")
        self.assertIn("Screen", package.joinpath("terminal.tcss").read_text())
        sample = json.loads(package.joinpath("data/slips_sample.json").read_text())
        self.assertTrue(sample["data"])
        self.assertIn("Synthetic", sample["message"])

    def test_default_command_starts_terminal(self):
        with patch("iloveedging.__main__.Settings.load", return_value=Settings()), patch("iloveedging.__main__.cmd_tui", return_value=0) as terminal:
            self.assertEqual(main([]), 0)
            arguments = terminal.call_args.args[0]
            self.assertEqual(arguments.port, 8787)
            self.assertFalse(arguments.connect_only)

    def test_terminal_options_work_without_subcommand(self):
        with patch("iloveedging.__main__.Settings.load", return_value=Settings()), patch("iloveedging.__main__.cmd_tui", return_value=0) as terminal:
            main(["--port", "8788", "--interval", "60", "--connect-only"])
            arguments = terminal.call_args.args[0]
            self.assertEqual((arguments.port, arguments.interval, arguments.connect_only), (8788, 60, True))

    def test_web_command_is_removed(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["serve"])
        self.assertEqual(error.exception.code, 2)

    def test_service_never_serves_browser_or_private_files(self):
        engine = Mock()
        handler_class = make_handler(engine)
        handler = object.__new__(handler_class)
        handler._json = Mock()
        for path in ("/", "/index.html", "/.env", "/../../.env", "/assets/app.js"):
            handler.path = path
            handler.do_GET()
            self.assertEqual(handler._json.call_args.args[1], 404)
        engine.state.assert_not_called()
        handler.path = "/api/state"
        engine.state.return_value = {"scanning": True}
        handler.do_GET()
        handler._json.assert_called_with({"scanning": True})


if __name__ == "__main__":
    unittest.main()
