import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from rca_agent import run_analysis
from rca_agent.control import RunControl
from rca_agent.report import RunResult


def tool_call(name: str, arguments: Any, call_id: str = "call-1") -> dict[str, Any]:
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
        }}],
    }}]}


def conclusion(file_id: str, extra_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    report = {
        "root_cause_found": False, "information_gaps": ["尚缺获准测试的验证结果。"],
        "follow_up_questions": ["能否提供实际输入和测试结果？"],
        "candidate_causes": [{
            "summary": "固定除数可能导致三项输入的平均值错误，需要后续测试验证。",
            "supporting_evidence": [
                {"evidence_id": file_id, "start_line": 2, "end_line": 2},
                *({"evidence_id": value} for value in extra_ids),
            ],
            "contradicting_evidence": [],
        }],
        "verification": [], "unconfirmed": ["本次没有运行测试。"],
        "repair_suggestions": ["核对实际输入数量。"], "next_steps": ["运行获准的验证。"],
    }
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(report, ensure_ascii=False),
    }}]}


class ScriptedModel:
    def __init__(self, responses: list[dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
        control.checkpoint()
        self.requests.append(json.loads(json.dumps(request)))
        response = self.responses.pop(0)
        return response(request) if callable(response) else response

    def feedback(self) -> list[dict[str, Any]]:
        return [message for message in self.requests[-1]["messages"] if message["role"] == "tool"]


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src/average.py").write_text(
            "def average(values):\n    return sum(values) / 2\n", encoding="utf-8"
        )
        self.config: dict[str, Any] = {
            "question": "三项输入的平均值为什么错误？",
            "initial_materials": ["用户报告平均值不正确，尚未验证。"],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"max_model_calls": 12, "max_tool_calls": 12, "total_seconds": 5},
        }

    def investigate(self, *responses: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[RunResult, ScriptedModel]:
        model = ScriptedModel(list(responses))
        return run_analysis(self.config, transport=model), model

    def test_directory_search_read_feedback_forms_evidence_report(self) -> None:
        def choose_search(request: dict[str, Any]) -> dict[str, Any]:
            feedback = json.loads(request["messages"][-1]["content"])
            listing = json.loads(feedback["evidence"]["content"])
            path = next(row["path"] for row in listing["entries"] if row["path"].endswith("average.py"))
            return tool_call("search_text", {"path": path, "query": "sum"}, "search")

        def choose_read(request: dict[str, Any]) -> dict[str, Any]:
            feedback = json.loads(request["messages"][-1]["content"])
            match = json.loads(feedback["evidence"]["content"])["matches"][0]
            return tool_call("read_file", {"path": match["path"], "start_line": 1, "end_line": match["line"]}, "read")

        result, model = self.investigate(
            tool_call("list_directory", {"path": "."}, "directory"),
            choose_search,
            choose_read,
            conclusion("E4", ("E2", "E3")),
        )
        messages = model.feedback()
        self.assertTrue(json.loads(messages[0]["content"])["ok"])
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual([e.kind for e in result.evidence], ["initial_material", "directory", "search", "file"])
        listing = json.loads(result.evidence[1].content)
        self.assertIn({"path": "src/average.py", "type": "file"}, listing["entries"])
        search = json.loads(result.evidence[2].content)
        self.assertEqual(search["matches"], [{"path": "src/average.py", "line": 2, "text": "    return sum(values) / 2\n"}])
        self.assertTrue(search["scan_complete"])
        for message, item in zip(messages, result.evidence[1:], strict=True):
            self.assertEqual(json.loads(message["content"])["evidence"]["content"], item.content)
        self.assertEqual([m["tool_call_id"] for m in messages], ["directory", "search", "read"])
        self.assertEqual({t["function"]["name"] for t in model.requests[0]["tools"]},
                         {"list_directory", "search_text", "read_file", "run_test", "ask_user"})
        self.assertIn("宿主目录查看", result.markdown)
        self.assertIn("宿主文本搜索", result.markdown)

    def test_three_tools_reject_invalid_and_external_targets_with_paired_feedback(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("EXTERNAL_CONTENT_NOT_ALLOWED", encoding="utf-8")
        (self.root / "outside-directory").symlink_to(outside, target_is_directory=True)
        (self.root / "outside-file").symlink_to(outside / "secret.txt")
        cases: list[tuple[str, Any, str]] = [
            ("list_directory", {"path": " "}, "invalid_arguments"),
            ("list_directory", {"path": ".", "root": str(outside)}, "invalid_arguments"),
            ("search_text", {"path": ".", "query": ""}, "invalid_arguments"),
            ("search_text", {"path": ".", "query": "x", "regex": True}, "invalid_arguments"),
            ("read_file", {"path": " ", "start_line": 1, "end_line": 1}, "invalid_arguments"),
            ("read_file", {"path": "src/average.py", "start_line": True, "end_line": 2}, "invalid_arguments"),
        ]
        for directory_path, file_path in ((str(outside), str(outside / "secret.txt")),
                                           ("../outside", "../outside/secret.txt"),
                                           ("outside-directory", "outside-file")):
            cases.extend([
                ("list_directory", {"path": directory_path}, "permission_denied"),
                ("search_text", {"path": file_path, "query": "EXTERNAL"}, "permission_denied"),
                ("read_file", {"path": file_path, "start_line": 1, "end_line": 1}, "permission_denied"),
            ])
        for name, arguments, error in cases:
            with self.subTest(tool=name, arguments=arguments):
                result, model = self.investigate(
                    tool_call(name, arguments, "rejected"),
                    tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}, "recovery"),
                    conclusion("E2"),
                )
                self.assertEqual(result.status, "completed", result.reason)
                self.assertEqual(result.tool_calls, 2)
                feedback = model.feedback()
                self.assertEqual([m["tool_call_id"] for m in feedback], ["rejected", "recovery"])
                self.assertEqual(json.loads(feedback[0]["content"])["error"], error)
                self.assertFalse(any("EXTERNAL_CONTENT_NOT_ALLOWED" in item.content for item in result.evidence))
                self.assertEqual(len(result.evidence), 2)

    def test_recursive_links_are_skipped_and_internal_file_link_can_be_searched(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "hidden.txt").write_text("EXTERNAL_CONTENT_NOT_ALLOWED sum", encoding="utf-8")
        (self.root / "external-file").symlink_to(outside / "hidden.txt")
        (self.root / "external-directory").symlink_to(outside, target_is_directory=True)
        (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        (self.root / "broken").symlink_to("missing-target")
        (self.root / "alias.py").symlink_to(self.root / "src/average.py")
        result, model = self.investigate(
            tool_call("list_directory", {"path": "."}),
            tool_call("search_text", {"path": ".", "query": "sum"}),
            tool_call("read_file", {"path": "alias.py", "start_line": 1, "end_line": 2}),
            conclusion("E4", ("E2", "E3")),
        )
        self.assertEqual(result.status, "completed", result.reason)
        for item in result.evidence[1:3]:
            data = json.loads(item.content)
            self.assertTrue(data["scan_complete"])
            self.assertEqual(data["skipped_count"], 4)
            self.assertEqual(data["skipped_reasons"], {
                "permission_denied": 2, "directory_symlink": 1, "unresolvable_link": 1,
            })
            self.assertLess(data["scanned_entries"], 10)
        matches = json.loads(result.evidence[2].content)["matches"]
        self.assertEqual({m["path"] for m in matches}, {"alias.py", "src/average.py"})
        self.assertEqual(result.evidence[3].source, "src/average.py")
        self.assertNotIn("EXTERNAL_CONTENT_NOT_ALLOWED", result.markdown)
        self.assertTrue(all(json.loads(m["content"])["ok"] for m in model.feedback()))

    def test_model_can_choose_another_legal_order_and_authorized_absolute_path(self) -> None:
        result, _ = self.investigate(
            tool_call("read_file", {"path": str(self.root / "src/average.py"), "start_line": 1, "end_line": 2}),
            tool_call("search_text", {"path": str(self.root / "src/average.py"), "query": "sum"}),
            tool_call("list_directory", {"path": str(self.root / "src")}),
            conclusion("E2", ("E3", "E4")),
        )
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual([e.kind for e in result.evidence[1:]], ["file", "search", "directory"])

    def limited_search(self, path: str, query: str) -> tuple[RunResult, ScriptedModel, dict[str, Any]]:
        result, model = self.investigate(
            tool_call("search_text", {"path": path, "query": query}),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
            conclusion("E3", ("E2",)),
        )
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(json.loads(model.feedback()[0]["content"])["evidence"]["content"], result.evidence[1].content)
        return result, model, json.loads(result.evidence[1].content)

    def test_zero_matches_at_scan_limit_is_not_a_complete_negative_search(self) -> None:
        self.config["limits"]["max_scan_entries"] = 1
        result, _, data = self.limited_search(".", "sum")
        self.assertEqual(data["matches"], [])
        self.assertEqual(data["scanned_entries"], 1)
        self.assertEqual(data["stop_reason"], "scan_limit")
        self.assertFalse(data["scan_complete"])
        self.assertFalse(data["results_truncated"])
        self.assertEqual(data["skipped_count"], 0)
        self.assertIn("scan_limit", result.markdown)

    def test_search_byte_budget_does_not_publish_a_partial_line(self) -> None:
        self.config["limits"]["max_search_bytes"] = 10
        result, _, data = self.limited_search("src/average.py", "def")
        self.assertEqual(data["read_bytes"], 10)
        self.assertEqual(data["matches"], [])
        self.assertEqual(data["stop_reason"], "read_limit")
        self.assertFalse(data["scan_complete"])
        self.assertIn("read_limit", result.markdown)

    def test_long_line_skip_is_bounded_and_later_full_line_can_match(self) -> None:
        (self.root / "long.txt").write_text("needle" + "x" * 100 + "\nneedle\n", encoding="utf-8")
        self.config["limits"]["max_line_bytes"] = 16
        for byte_limit in (32, 1024):
            with self.subTest(byte_limit=byte_limit):
                self.config["limits"]["max_search_bytes"] = byte_limit
                result, _, data = self.limited_search("long.txt", "needle")
                self.assertLessEqual(data["read_bytes"], byte_limit)
                self.assertEqual(data["skipped_reasons"], {"line_limit": 1})
                self.assertEqual(data["skipped"][0]["line"], 1)
                self.assertIn("line_limit", result.markdown)
                if byte_limit == 32:
                    self.assertFalse(data["scan_complete"])
                    self.assertEqual(data["matches"], [])
                    self.assertEqual(data["stop_reason"], "read_limit")
                else:
                    self.assertTrue(data["scan_complete"])
                    self.assertEqual(data["matches"], [{"path": "long.txt", "line": 2, "text": "needle\n"}])

    def test_directory_and_search_stop_when_result_limit_is_reached(self) -> None:
        self.config["limits"]["max_results"] = 1
        for name, arguments, field in (("list_directory", {"path": "."}, "entries"),
                                        ("search_text", {"path": "src/average.py", "query": " "}, "matches")):
            with self.subTest(tool=name):
                result, _ = self.investigate(
                    tool_call(name, arguments),
                    tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
                    conclusion("E3", ("E2",)),
                )
                self.assertEqual(result.status, "completed", result.reason)
                data = json.loads(result.evidence[1].content)
                self.assertEqual(len(data[field]), 1)
                self.assertEqual(data["returned_count"], 1)
                self.assertEqual(data["stop_reason"], "results_limit")
                self.assertFalse(data["scan_complete"])
                self.assertFalse(data["results_truncated"])

    def test_feedback_limit_counts_json_wrapping_escaping_and_saved_metadata(self) -> None:
        line = "命中 " + '"\\' * 30 + "\n"
        (self.root / "escaped.txt").write_text(line * 30, encoding="utf-8")
        self.config["limits"]["max_tool_feedback_bytes"] = 2048
        result, model, data = self.limited_search("escaped.txt", "命中")
        self.assertGreater(len(data["matches"]), 0)
        self.assertLess(len(data["matches"]), 30)
        self.assertTrue(data["results_truncated"])
        self.assertFalse(data["scan_complete"])
        self.assertEqual(data["stop_reason"], "feedback_limit")
        self.assertIn("feedback_limit", result.markdown)
        for feedback in model.feedback():
            self.assertLessEqual(len(feedback["content"].encode("utf-8")), 2048)
        self.assertGreater(len(model.feedback()[0]["content"].encode("utf-8")),
                           sum(len(m["text"].encode("utf-8")) for m in data["matches"]))

    def test_many_skips_keep_bounded_details_and_complete_reason_counts(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("not authorized", encoding="utf-8")
        for index in range(50):
            (self.root / f"external-{index:02d}-{'名' * 20}").symlink_to(outside)
        self.config["limits"]["max_tool_feedback_bytes"] = 2048
        result, model = self.investigate(
            tool_call("list_directory", {"path": "."}),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
            conclusion("E3", ("E2",)),
        )
        self.assertEqual(result.status, "completed", result.reason)
        data = json.loads(result.evidence[1].content)
        self.assertTrue(data["scan_complete"])
        self.assertEqual(data["skipped_count"], 50)
        self.assertEqual(data["skipped_reasons"], {"permission_denied": 50})
        self.assertTrue(data["skipped_details_omitted"])
        self.assertLess(len(data["skipped"]), 50)
        self.assertTrue(any(row["path"] == "src/average.py" for row in data["entries"]))
        self.assertLessEqual(len(model.feedback()[0]["content"].encode("utf-8")), 2048)

    def test_empty_search_and_real_target_failure_keep_distinct_feedback_and_prior_evidence(self) -> None:
        vanished = self.root / "vanished.txt"
        vanished.write_text("previously listed", encoding="utf-8")

        def delete_then_search(request: dict[str, Any]) -> dict[str, Any]:
            vanished.unlink()
            return tool_call("search_text", {"path": "vanished.txt", "query": "listed"}, "missing")

        result, model = self.investigate(
            tool_call("list_directory", {"path": "."}, "before-delete"),
            delete_then_search,
            tool_call("search_text", {"path": "src/average.py", "query": "ABSENT"}, "empty"),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}, "recover"),
            conclusion("E4", ("E2", "E3")),
        )
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(result.tool_calls, 4)
        feedback = model.feedback()
        self.assertEqual([m["tool_call_id"] for m in feedback], ["before-delete", "missing", "empty", "recover"])
        self.assertEqual(json.loads(feedback[1]["content"])["error"], "file_unavailable")
        empty = json.loads(json.loads(feedback[2]["content"])["evidence"]["content"])
        self.assertTrue(empty["scan_complete"])
        self.assertEqual(empty["matches"], [])
        self.assertEqual(empty["skipped_count"], 0)
        self.assertIn("vanished.txt", result.evidence[1].content)
        self.assertEqual(len(result.evidence), 4)
        self.assertIn("file_unavailable", result.markdown)
        self.assertIn("missing", result.markdown)

    def test_three_snapshots_and_references_survive_source_changes(self) -> None:
        def change_material_then_conclude(request: dict[str, Any]) -> dict[str, Any]:
            (self.root / "src/average.py").write_text("modified\n", encoding="utf-8")
            (self.root / "new.txt").write_text("new material", encoding="utf-8")
            return conclusion("E4", ("E2", "E3"))

        result, model = self.investigate(
            tool_call("list_directory", {"path": "."}),
            tool_call("search_text", {"path": "src", "query": "sum"}),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
            change_material_then_conclude,
        )
        self.assertEqual(result.status, "completed", result.reason)
        self.assertNotIn("new.txt", result.evidence[1].content)
        self.assertIn("return sum(values) / 2", result.evidence[2].content)
        self.assertIn("return sum(values) / 2", result.evidence[3].content)
        for message, item in zip(model.feedback(), result.evidence[1:], strict=True):
            self.assertEqual(json.loads(message["content"])["evidence"]["content"], item.content)
        for item in result.evidence[1:3]:
            self.assertIsNone(item.start_line)
            self.assertIsNone(item.end_line)
        self.assertEqual(json.loads(result.evidence[2].content)["query"], "sum")
        self.assertEqual(result.evidence[2].source, "src")
        output = Path(self.temp.name) / "snapshot-report"
        result.write_to(output)
        saved = json.loads((output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["evidence"][2]["content"], result.evidence[2].content)
        self.assertNotIn("modified", (output / "report.md").read_text(encoding="utf-8"))

    def test_non_file_ranges_are_rejected_and_repair_describes_reference_kinds(self) -> None:
        for invalid_kind in ("directory", "search", "only_directory"):
            with self.subTest(invalid_kind=invalid_kind):
                invalid = conclusion("E4", ("E2", "E3"))
                report = json.loads(invalid["choices"][0]["message"]["content"])
                support = report["candidate_causes"][0]["supporting_evidence"]
                if invalid_kind == "only_directory":
                    report["candidate_causes"][0]["supporting_evidence"] = [{"evidence_id": "E2"}]
                else:
                    support[1 if invalid_kind == "directory" else 2].update({"start_line": 1, "end_line": 1})
                invalid["choices"][0]["message"]["content"] = json.dumps(report)
                result, model = self.investigate(
                    tool_call("list_directory", {"path": "."}),
                    tool_call("search_text", {"path": "src", "query": "sum"}),
                    tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
                    invalid, invalid,
                )
                self.assertEqual(result.status, "system_failure")
                self.assertIn("missing_file_evidence" if invalid_kind == "only_directory"
                              else "invalid_non_file_reference", result.reason)
                repair = json.loads(model.requests[-1]["messages"][-1]["content"])
                directory = next(e for e in repair["available_evidence"] if e["kind"] == "directory")
                self.assertEqual(directory["reference_fields"], ["evidence_id"])
                self.assertNotIn("start_line", directory)
                self.assertEqual(len(result.evidence), 4)

    def test_non_utf8_subfile_is_skipped_but_direct_target_is_a_tool_error(self) -> None:
        (self.root / "binary.txt").write_bytes(b"sum before corrupt\n\xff\n")
        result, _, data = self.limited_search(".", "sum")
        self.assertTrue(data["scan_complete"])
        self.assertEqual(data["skipped_reasons"], {"non_utf8": 1})
        self.assertTrue(all(match["path"] != "binary.txt" for match in data["matches"]))
        self.assertIn("non_utf8", result.markdown)
        failed, model = self.investigate(
            tool_call("search_text", {"path": "binary.txt", "query": "sum"}, "bad-file"),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
            conclusion("E2"),
        )
        self.assertEqual(failed.status, "completed", failed.reason)
        self.assertEqual(json.loads(model.feedback()[0]["content"])["error"], "file_unavailable")
        self.assertEqual(len(failed.evidence), 2)

    def test_literal_search_preserves_case_and_nonempty_whitespace_query(self) -> None:
        for query, expected_lines in (("sum", [2]), ("SUM", []), ("sum.*", []), (" ", [1, 2])):
            with self.subTest(query=query):
                _, _, data = self.limited_search("src/average.py", query)
                self.assertEqual([match["line"] for match in data["matches"]], expected_lines)
                self.assertEqual(data["query"], query)
                self.assertTrue(data["scan_complete"])

    def test_file_feedback_and_errors_also_obey_serialized_byte_budget(self) -> None:
        (self.root / "escaped.py").write_text('"' * 1000 + "\n", encoding="utf-8")
        self.config["limits"]["max_tool_feedback_bytes"] = 2048
        result, model = self.investigate(
            tool_call("read_file", {"path": "escaped.py", "start_line": 1, "end_line": 1}, "too-big"),
            tool_call("read_file", {"path": "src/average.py", "start_line": 1, "end_line": 2}),
            conclusion("E2"),
        )
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(json.loads(model.feedback()[0]["content"])["error"], "feedback_limit")
        self.assertEqual(len(result.evidence), 2)
        for feedback in model.feedback():
            self.assertLessEqual(len(feedback["content"].encode("utf-8")), 2048)

    def test_new_limits_are_validated_before_model_communication(self) -> None:
        for name in ("max_scan_entries", "max_search_bytes", "max_line_bytes", "max_results", "max_tool_feedback_bytes"):
            for invalid in (False, 0, -1, 1.5):
                with self.subTest(limit=name, invalid=invalid):
                    config = {**self.config, "limits": {name: invalid}}
                    model = ScriptedModel([])
                    with self.assertRaises(ValueError):
                        run_analysis(config, transport=model)
                    self.assertEqual(model.requests, [])
        model = ScriptedModel([])
        with self.assertRaisesRegex(ValueError, "2048"):
            run_analysis({**self.config, "limits": {"max_tool_feedback_bytes": 2047}}, transport=model)
        self.assertEqual(model.requests, [])

    def test_internal_failure_keeps_prior_snapshot_instead_of_returning_empty_success(self) -> None:
        def communication_failure(request: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("simulated model communication failure")

        result, _ = self.investigate(tool_call("list_directory", {"path": "."}), communication_failure)
        self.assertEqual(result.status, "system_failure")
        self.assertEqual(result.reason, "unexpected_error")
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(result.evidence[1].kind, "directory")
        self.assertIn("src/average.py", result.evidence[1].content)
        self.assertIsNone(result.report)
