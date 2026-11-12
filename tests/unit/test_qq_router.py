"""规则路由测试：顺序、意图、难负例。"""

import unittest

from src.qq.router import classify


class ClassifyCase(unittest.TestCase):
    def test_status_command(self):
        for text in ("/status", "status", "在吗"):
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result["intent"], "status")
                self.assertEqual(result["action"], "execute")

    def test_ask_command(self):
        result = classify("/ask 1+1 等于几")

        self.assertEqual(result["intent"], "ask")
        self.assertEqual(result["params"]["question"], "1+1 等于几")

    def test_task_command(self):
        result = classify("/task 做一个扫雷")

        self.assertEqual(result["intent"], "task")
        self.assertEqual(result["action"], "execute")
        self.assertEqual(result["params"]["task"], "做一个扫雷")

    def test_task_without_text_needs_clarify(self):
        result = classify("/task")

        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["action"], "clarify")

    def test_cancel_command(self):
        result = classify("/cancel 123")

        self.assertEqual(result["intent"], "cancel")
        self.assertEqual(result["params"]["target"], "123")

    def test_confirm_command(self):
        result = classify("/confirm K7M2P9QX")

        self.assertEqual(result["intent"], "confirm")
        self.assertEqual(result["params"]["confirmation_id"], "K7M2P9QX")

    def test_danger_before_everything(self):
        for text in ("执行 rm -rf /", "删除整个目录", "格式化磁盘"):
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result["action"], "deny")
                self.assertEqual(result["intent"], "exec")

    def test_read_file_with_path(self):
        result = classify("读一下 reports/today.md")

        self.assertEqual(result["intent"], "read_file")
        self.assertEqual(result["params"]["path"], "reports/today.md")
        self.assertEqual(result["action"], "confirm")

    def test_read_file_without_path_is_clarify(self):
        result = classify("读一下这个")

        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["reason"], "need_path")

    def test_image_is_unavailable_not_clarify(self):
        result = classify("画一只戴帽子的猫")

        self.assertEqual(result["action"], "unavailable")

    def test_plain_text_is_ask(self):
        result = classify("1+1 等于几")

        self.assertEqual(result["intent"], "ask")
        self.assertEqual(result["action"], "execute")
        self.assertEqual(result["reason"], "plain_text_ask")

    def test_unknown_slash_command(self):
        result = classify("/statusx")

        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["reason"], "unknown_command")

    def test_empty_text(self):
        result = classify("   ")

        self.assertEqual(result["action"], "clarify")

    def test_question_with_file_word_not_read_file(self):
        result = classify("文件系统是什么？")

        self.assertEqual(result["intent"], "ask")

    def test_ask_without_question(self):
        result = classify("/ask")

        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["reason"], "ask_without_question")

    def test_case_insensitive_danger(self):
        result = classify("RM -RF /")

        self.assertEqual(result["action"], "deny")

    def test_write_file_is_unavailable(self):
        result = classify("把这段存成 output/a.md")

        self.assertEqual(result["action"], "unavailable")



    def test_confirm_without_id(self):
        result = classify("/confirm")

        self.assertEqual(result["intent"], "confirm")
        self.assertEqual(result["params"]["confirmation_id"], "")

    def test_format_command_denied(self):
        result = classify("格式化整个磁盘")

        self.assertEqual(result["action"], "deny")



    def test_reset_command(self):
        self.assertEqual(classify("/reset")["intent"], "reset")

    def test_continue_uses_context(self):
        result = classify("继续，说明怎么算的")

        self.assertEqual(result["intent"], "ask")
        self.assertTrue(result["use_context"])

    def test_continue_read_file_keeps_priority(self):
        result = classify("继续读取 reports/today.md")

        self.assertEqual(result["intent"], "read_file")
        self.assertFalse(result["use_context"])



    def test_continue_run_command(self):
        run_id = "12345678-1234-1234-1234-123456789012"
        result = classify(f"/continue {run_id} 再补一段")

        self.assertEqual(result["intent"], "continue_run")
        self.assertEqual(result["params"]["run_id"], run_id)
        self.assertEqual(result["params"]["message"], "再补一段")

    def test_continue_run_usage(self):
        result = classify("/continue 12345678")

        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["reason"], "continue_usage")


if __name__ == "__main__":
    unittest.main()
