import csv
import contextlib
import io
import json
import tempfile
import unittest
import unicodedata
from pathlib import Path

import refresh_serbian_deck as deck


class SerbianDeckPipelineTests(unittest.TestCase):
    def write_source(self, path: Path, records: list[list[str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            for header in deck.ANKI_HEADERS:
                handle.write(header + "\n")
            writer = csv.writer(
                handle,
                delimiter="\t",
                quotechar='"',
                lineterminator="\n",
            )
            writer.writerows(records)

    def test_parser_handles_quoted_multiline_tab_and_nonstandard_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            self.write_source(
                source,
                [
                    [
                        "guid-1",
                        "<div>Ćošak</div><div>[sound:old.mp3]<br></div>",
                        "первая строка\nвторая\tстрока",
                        "leech",
                    ]
                ],
            )

            headers, notes = deck.read_anki_export(source, expected_records=None)

            self.assertEqual(headers, list(deck.ANKI_HEADERS))
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].serbian_plain, "Ćošak")
            self.assertEqual(notes[0].russian_plain, "первая строка вторая строка")
            self.assertEqual(notes[0].old_audio, "old.mp3")
            self.assertEqual(notes[0].tags, "leech")

    def test_real_source_multiline_record_is_one_note(self) -> None:
        source = Path(__file__).resolve().parents[1] / "srpski.txt"
        _, notes = deck.read_anki_export(source)
        by_guid = {note.guid: note for note in notes}

        self.assertEqual(len(notes), deck.EXPECTED_RECORDS)
        self.assertIn("HrRFC4=B/1", by_guid)
        self.assertEqual(by_guid["HrRFC4=B/1"].serbian_plain, "Za jednokratnu upotrebu")
        self.assertEqual(by_guid["HrRFC4=B/1"].russian_plain, "Одноразовый")
        self.assertEqual(by_guid["HrRFC4=B/1"].old_audio, "")

    def test_duplicate_guids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            self.write_source(
                source,
                [
                    ["same", "Prvi", "Первый", ""],
                    ["same", "Drugi", "Второй", ""],
                ],
            )

            with self.assertRaisesRegex(deck.DeckError, "duplicate GUID"):
                deck.read_anki_export(source, expected_records=None)

    def test_review_writer_round_trips_quotes_tabs_and_unicode(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update(
            {
                "index": "1",
                "guid": 'guid-"quoted"',
                "serbian_original": "Ćošak\tsa navodnikom \"da\"",
                "russian_original": "Угол",
                "tags": "leech",
                "status": "draft",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory) / "review.tsv"
            deck.atomic_write_review(review, [row])

            loaded = deck.read_review(review)

        self.assertEqual(loaded, [row])

    def test_review_reader_rejects_extra_unquoted_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory) / "review.tsv"
            review.write_text(
                "\t".join(deck.REVIEW_FIELDS) + "\n" + "\t".join(["x"] * 13) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(deck.DeckError, "exactly 12 fields"):
                deck.read_review(review)

    def test_anki_writer_preserves_headers_fields_quotes_and_order(self) -> None:
        records = [
            ["g1", "Ćao<br>[sound:a.mp3]", 'Он сказал: "Привет"', ""],
            ["g2", "Pitanje?<br>[sound:b.mp3]", "Вопрос?", "leech"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "anki.txt"
            deck.atomic_write_anki(output, deck.ANKI_HEADERS, records)

            headers, loaded = deck.read_output_records(output)

        self.assertEqual(headers, list(deck.ANKI_HEADERS))
        self.assertEqual(loaded, records)

    def test_audio_filename_is_deterministic_ascii_safe_and_text_sensitive(self) -> None:
        first = deck.audio_filename("g<1", "Sačekaj me na ćošku")
        decomposed = unicodedata.normalize("NFD", "Sačekaj me na ćošku")
        same_normalized = deck.audio_filename("g<1", decomposed)
        changed = deck.audio_filename("g<1", "Sačekaj me kod apoteke")

        self.assertEqual(first, same_normalized)
        self.assertNotEqual(first, changed)
        self.assertIn("sacekaj_me_na_cosku", first)
        self.assertRegex(first, r"^sr_[a-z0-9_]+_[0-9a-f]{8}_[0-9a-f]{8}\.mp3$")

    def test_html_escaping_leaves_no_raw_user_markup(self) -> None:
        self.assertEqual(
            deck.escaped_card_text("Pitaj <Miku> & sačekaj"),
            "Pitaj &lt;Miku&gt; &amp; sačekaj",
        )

    def test_text_validation_rejects_period_and_cyrillic_but_keeps_question(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update(
            {
                "guid": "g1",
                "serbian_final": "Sačekaj me na углу.",
                "russian_final": "Подожди меня на углу.",
                "change_kind": "expanded",
                "status": "approved",
            }
        )
        issues = deck.row_text_issues(row, allow_incomplete=False)
        messages = [issue.message for issue in issues]
        self.assertIn("contains Cyrillic; use Serbian Latin", messages)
        self.assertEqual(messages.count("omit the sentence-final full stop"), 2)

        row["serbian_final"] = "Hoćeš li da sačekaš ovde?"
        row["russian_final"] = "Подождёшь здесь?"
        issues = deck.row_text_issues(row, allow_incomplete=False)
        self.assertFalse(any("full stop" in issue.message for issue in issues))

    def test_text_validation_warns_on_masculine_first_person_only(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update(
            {
                "guid": "g1",
                "serbian_final": "Nisam znao da si već stigla",
                "russian_final": "Я не знал, что ты уже пришла",
                "change_kind": "expanded",
                "status": "approved",
            }
        )
        issues = deck.row_text_issues(row, allow_incomplete=False)
        self.assertEqual(sum("speaker must be female" in issue.message for issue in issues), 2)

        row["serbian_final"] = "On nije znao da sam već stigla"
        row["russian_final"] = "Он не знал, что я уже пришла"
        issues = deck.row_text_issues(row, allow_incomplete=False)
        self.assertFalse(any("speaker must be female" in issue.message for issue in issues))

    def test_audio_review_accept_marks_card_verified(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update({"guid": "g1", "status": "audio_done"})

        deck.apply_audio_review_decision(row, "accept")

        self.assertEqual(row["status"], "verified")

    def test_audio_review_change_request_preserves_note_and_reopens_card(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update({"guid": "g1", "status": "audio_done", "note": "Earlier note"})

        deck.apply_audio_review_decision(row, "change_request", "use a shorter pause")

        self.assertEqual(row["status"], "needs_review")
        self.assertEqual(row["note"], "Earlier note | Audio review: use a shorter pause")

    def test_audio_review_selection_resumes_in_source_order(self) -> None:
        rows = []
        for index, status in enumerate(
            ["verified", "audio_done", "needs_review", "audio_done"], start=1
        ):
            row = {field: "" for field in deck.REVIEW_FIELDS}
            row.update({"index": str(index), "guid": f"g{index}", "status": status})
            rows.append(row)

        selected = deck.select_audio_review_rows(
            rows,
            start_index=2,
            end_index=None,
            limit=None,
            include_verified=False,
        )

        self.assertEqual([row["guid"] for row in selected], ["g2", "g4"])

    def test_audio_review_defaults_to_a_short_end_of_clip_input_window(self) -> None:
        args = deck.build_parser().parse_args(["review-audio"])

        self.assertEqual(
            args.input_window_seconds,
            deck.DEFAULT_AUDIO_REVIEW_INPUT_WINDOW_SECONDS,
        )
        self.assertLessEqual(args.input_window_seconds, 1.0)

    def test_playback_window_buffers_acceptance_and_guards_quit(self) -> None:
        self.assertEqual(deck.interpret_playback_window_key("a", None), ("a", None))
        self.assertEqual(deck.interpret_playback_window_key("\n", None), ("", None))

        choice, pending = deck.interpret_playback_window_key("q", None)
        self.assertIsNone(choice)
        self.assertEqual(pending, "q")
        self.assertEqual(deck.interpret_playback_window_key("\n", pending), ("q", None))

    def test_audio_review_display_compares_original_and_new_note(self) -> None:
        row = {field: "" for field in deck.REVIEW_FIELDS}
        row.update(
            {
                "index": "101",
                "guid": "g1",
                "serbian_original": "Prići, -đem, -đu",
                "russian_original": "Подойти",
                "serbian_final": "Prišla sam šalteru",
                "russian_final": "Я подошла к окошку",
            }
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            deck.print_audio_review_card(row, 1, 100, Path("new-audio.mp3"))

        rendered = output.getvalue()
        self.assertIn("Original Serbian: Prići, -đem, -đu", rendered)
        self.assertIn("Original Russian: Подойти", rendered)
        self.assertIn("New Serbian:      Prišla sam šalteru", rendered)
        self.assertIn("New Russian:      Я подошла к окошку", rendered)

    def test_tts_override_affects_only_its_effective_config(self) -> None:
        base = {"voice": "Leda", "speaking_rate": 1.0}
        override = {"prompt": "Pronounce frižider clearly", "review_tag": "tts-revisit"}

        unchanged = deck.effective_tts_config(base, None)
        changed = deck.effective_tts_config(base, override)

        self.assertEqual(unchanged, base)
        self.assertEqual(changed["prompt"], override["prompt"])
        self.assertNotIn("prompt", base)

    def test_tts_override_file_requires_a_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.json"
            path.write_text(json.dumps({"g1": {"review_tag": "tts-revisit"}}), encoding="utf-8")

            with self.assertRaisesRegex(deck.DeckError, "has no prompt"):
                deck.load_tts_overrides(path)

    def test_synthesis_parser_accepts_an_explicit_source_range(self) -> None:
        args = deck.build_parser().parse_args(
            ["synthesize", "--start-index", "101", "--end-index", "200", "--dry-run"]
        )

        self.assertEqual(args.start_index, 101)
        self.assertEqual(args.end_index, 200)

    def test_synthesis_defaults_leave_provider_quota_headroom(self) -> None:
        args = deck.build_parser().parse_args(["synthesize", "--dry-run"])

        self.assertEqual(args.requests_per_minute, deck.DEFAULT_TTS_REQUESTS_PER_MINUTE)
        self.assertEqual(args.requests_per_minute, 12.0)
        self.assertEqual(args.retries, deck.DEFAULT_TTS_RETRIES)


if __name__ == "__main__":
    unittest.main()
