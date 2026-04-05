import unittest
from io import StringIO

from yd2dbx.progress import format_progress_line, format_stage_line, print_done, print_phase, print_stage


class ProgressTests(unittest.TestCase):
    def test_format_progress_line_renders_terminal_prefix(self) -> None:
        self.assertEqual(format_progress_line("Sync", "42/150"), "\r\033[K[Sync] 42/150")

    def test_print_helpers_use_expected_line_endings(self) -> None:
        phase_stream = StringIO()
        done_stream = StringIO()
        stage_stream = StringIO()

        print_phase("Yandex", "1000 files", stream=phase_stream)
        print_done("Sync", "done", stream=done_stream)
        print_stage("Checks", "Проверяю доступ к API", stream=stage_stream)

        self.assertEqual(phase_stream.getvalue(), "\r\033[K[Yandex] 1000 files")
        self.assertEqual(done_stream.getvalue(), "\r\033[K[Sync] done\n")
        self.assertEqual(stage_stream.getvalue(), "[Checks] Проверяю доступ к API\n")

    def test_format_stage_line_renders_plain_message(self) -> None:
        self.assertEqual(format_stage_line("Report", "Пишу итоговый отчёт"), "[Report] Пишу итоговый отчёт")


if __name__ == "__main__":
    unittest.main()
