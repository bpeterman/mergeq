import contextlib
import io
import tempfile
import unittest
from unittest.mock import Mock, patch
from mergeq.cli import main


class CliTests(unittest.TestCase):
    def test_persistent_queue_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch("mergeq.cli.GitHub") as factory:
            factory.return_value.pr.return_value = {
                "state": "OPEN", "isDraft": False, "baseRefName": "main",
            }
            output = io.StringIO()
            def run(*args):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    return main([*args, "--repo", "someone/project", "--state-dir", directory])
            self.assertEqual(run("add", "12", "4", "12"), 0)
            self.assertEqual(run("status"), 0)
            self.assertIn("1. #12", output.getvalue())
            self.assertIn("2. #4", output.getvalue())
            self.assertEqual(run("remove", "12"), 0)
            self.assertEqual(run("retry", "4"), 0)
            self.assertEqual(run("stop"), 0)

    def test_invalid_batch_adds_nothing(self):
        with tempfile.TemporaryDirectory() as directory, patch("mergeq.cli.GitHub") as factory:
            factory.return_value.pr.side_effect = [
                {"state": "OPEN", "isDraft": False, "baseRefName": "main"},
                {"state": "CLOSED", "isDraft": False, "baseRefName": "main"},
            ]
            common = ["--repo", "someone/project", "--state-dir", directory]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["add", "12", "4", *common]), 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", *common]), 0)
            self.assertEqual(output.getvalue().strip(), "Queue empty.")
