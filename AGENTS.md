# Serbian Anki deck refresh

These instructions apply when working on the Serbian deck refresh. The goal is to replace the terse or faulty Serbian prompts in `srpski.txt` with short, natural examples useful for understanding both contemporary conversation and current news or other general-audience writing, regenerate Serbian audio, and produce a file that updates the existing Anki notes rather than creating a second deck.

## Source files and intent

- Treat `srpski.txt` as the immutable source export. It is an Anki tab-separated export with four header lines and four data columns: GUID, Serbian front plus audio markup, Russian back, and tags.
- Treat `sasha.txt` only as a reference for phrase length, everyday usefulness, and register. It contains alternating Portuguese and Russian lines; it is not an Anki import template and supplies no Serbian wording.
- Keep `main.py` unchanged as a read-only reference. It is not the Serbian deck pipeline: it currently targets English, reads a plain phrase list, automatically translates to Russian, does not preserve GUIDs or tags, derives fragile filenames from phrases, plays every generated clip, and produces output that cannot safely update this deck.
- Implement the new workflow in a separate CLI script named `refresh_serbian_deck.py`. Put reusable helpers in separate modules only when that materially improves testing or clarity; do not turn `main.py` into a wrapper around the new code.
- Do not edit `srpski.txt`, `sasha.txt`, credentials, or tokens in place. Write generated and intermediate artifacts beneath a dedicated ignored output directory such as `generated/`.
- Never print, copy, commit, or modify `client_secret.json` or `token.json` except when authentication itself is explicitly in scope.

The current source has 3,658 data records with 3,658 unique GUIDs. It contains a valid quoted multiline field, so never parse it with `splitlines()` plus `split("\t")`. Use Python's `csv` module with tab delimiters, quoting enabled, UTF-8, and `newline=""`.

## Required workflow

Build `refresh_serbian_deck.py` as a deterministic, resumable CLI with separate subcommands for the stages below. A stage must not silently trigger a later stage, and running the script without a subcommand must show help rather than do work.

1. **Extract** the source into a reviewable working table. Strip the existing `<br>[sound:...mp3]` markup from the Serbian text without altering the original export. Preserve source order, GUID, Russian text, tags, and old audio filename.
2. **Rewrite and review** the linguistic content in small batches. Store the original and proposed Serbian, original and proposed Russian, change category, review status, and reviewer note for every GUID. Resume by GUID and never overwrite an approved row.
3. **Validate text** offline before any paid TTS request. Invalid or unresolved rows go to a review report and are excluded from synthesis.
4. **Pilot audio** on a representative 20–30 approved cards, including `č`, `ć`, `š`, `ž`, `đ`, questions, numbers, and several sentence lengths. Get the user's approval of voice, speed, pronunciation, and card style before a full-deck synthesis run.
5. **Synthesize** only approved, changed, or missing clips. Cache by GUID, normalized Serbian text, voice/model, and audio settings. Retry transient failures with bounded backoff and produce a failure report; never leave a zero-byte file or a sound reference to a missing file.
6. **Review audio** sequentially with the Serbian phrase and Russian translation visible. Mark accepted recordings `verified`; store the user's exact requested change in the note and return that row to `needs_review`. Buffer decisions for the review session: `q` plus Enter saves and quits, `x` plus Enter or Ctrl-C discards and quits, and reaching the end saves automatically.
7. **Assemble** a new Anki TSV and a flat media directory. Do not shuffle records. Preserve the original headers, GUIDs, column order, and tags.
8. **Verify** the complete artifacts and produce an import report before telling the user they are ready.

Do not automate an import into the user's live Anki collection. The deliverable is a verified import TSV, its media files, a manifest, and explicit import instructions. The user controls the backup and live import.

## Serbian writing standard

- Use contemporary conversational Serbian as spoken in Serbia, in Latin script and **Ekavian** consistently: for example `vreme`, `lepo`, `devojka`, `ovde`. Do not mix in Ijekavian forms such as `vrijeme`, `lijepo`, `djevojka`, or `ovdje`.
- Preserve proper Serbian letters and normalize text to Unicode NFC: `č`, `ć`, `š`, `ž`, and `đ`. Never substitute ASCII `c`, `s`, `z`, or `dj` for them. Do not assume every `c` should become `ć`; correct from lexical and grammatical context.
- Correct more than diacritics. Repair spelling, morphology, agreement, aspect, government, punctuation, and false or unnatural expressions. Known source-like problems include forms such as `osečanja` where `osećanja` is intended, but every correction must be reasoned about individually.
- Prefer an ordinary phrase a Serbian speaker could plausibly say today. The surrounding sentence should remain natural and clear, but it may use a valid formal, literary, or news-oriented learning target retained from a single-word source card. Avoid invented literary narration, dictionary definitions, genuinely obsolete vocabulary, bureaucratic filler, and calques from Russian, Croatian, Bosnian, Montenegrin, or English when a normal Serbian expression exists.
- Aim for one self-contained sentence or utterance of roughly 5–12 words. A shorter line is acceptable for a genuinely idiomatic fixed expression; use at most 15 words unless meaning requires it. Usually use one clause, occasionally two short clauses.
- Omit a single sentence-final full stop from both the Serbian and Russian card text. Preserve `?`, `!`, `…`, and punctuation that belongs to an abbreviation or other expression; do not remove punctuation mechanically from the last character of every field.
- Preserve the useful learning target. When the original Serbian field is a single valid word, normally include that word naturally in the expanded sentence, inflected if needed, even if it is less conversational or belongs mainly to formal, literary, or news language. Broader recognition vocabulary is part of the deck's purpose. Replace it only when it is erroneous, genuinely obsolete enough to have little modern reading value, incompatible with Serbian/Ekavian usage, or not the meaning indicated by the Russian side, and record why. Apply the same preservation principle to valid multiword expressions when they remain useful.
- Preserve meaning, register, politeness, person, number, tense, and gender across Serbian and Russian. Rewrite the Russian side as a natural translation of the final Serbian sentence, not as a list of dictionary glosses.
- Treat the recurring card speaker as female, matching the selected female voice. Whenever a first-person form marks gender, use feminine Serbian and Russian (`odlučila sam` / `я решила`, `nisam znala` / `я не знала`, `zauzeta sam` / `я занята`). Other people in a sentence may naturally be any gender; do not mechanically feminize third-person subjects, addressees, or mixed groups.
- Do not use Google Translate or another machine translation as the authority for either language. It may be used only as a comparison signal. Resolve uncertain meaning with a reliable Serbian dictionary/corpus or mark the row `needs_review`; never guess silently.
- Avoid sensitive stereotypes and unnecessary violence when inventing context. Do not force every example into the same names, pronouns, or sentence frame.

Examples of the intended transformation shape (illustrative, not a batch to copy mechanically):

```text
Ćošak  ->  Sačekaj me na ćošku kod apoteke
Угол   ->  Подожди меня на углу у аптеки

Razlog ->  Nema razloga da se toliko brineš
Причина -> Нет причин так сильно переживать
```

## Review data and audit trail

Use one row per GUID in the working data. At minimum retain:

- `guid`
- `serbian_original`
- `russian_original`
- `serbian_final`
- `russian_final`
- `tags`
- `old_audio`
- `change_kind` (`expanded`, `orthography`, `grammar`, `meaning`, `modernized`, or a comma-separated combination)
- `status` (`draft`, `needs_review`, `approved`, `audio_done`, `verified`)
- `note`

Keep a machine-readable manifest that also records the source checksum, text checksum, TTS provider, model, voice, language code, audio settings, output filename, audio checksum, and error if any. Do not encode state only in filenames.

Work in batches of at most 100 notes. After each batch, run validation and save progress. Sample the beginning, middle, and end of each batch rather than reviewing only contiguous easy entries. Any material semantic change, ambiguous original, rare word, dialect doubt, or Serbian/Russian disagreement must remain `needs_review` until specifically resolved.

## Text validation gates

The validator must fail with actionable GUID-level messages when any of these checks fails:

- the data-record count differs from 3,658;
- GUIDs are missing, duplicated, invented, or reordered relative to the source;
- a record does not have exactly four fields after standards-compliant TSV parsing;
- tags differ from the source;
- a required Serbian or Russian field is blank;
- Serbian contains Cyrillic, replacement characters, tabs, raw newlines, or unescaped HTML;
- Serbian contains likely ASCII transliterations or known Ijekavian forms; these checks are warnings where lexical context is required, never blind search-and-replace rules;
- an approved sentence violates the configured length bounds without a recorded exception;
- the front contains anything besides the final Serbian text, exactly one `<br>`, and exactly one `[sound:filename.mp3]` reference;
- a referenced audio filename is unsafe, duplicated for different content, or absent from the media output;
- the Russian back does not correspond to the final Serbian sentence;
- temporary review metadata leaks into an Anki field.

Add unit tests for parsing and writing quoted tabs, quotes, Unicode diacritics, the source's quoted multiline record, HTML escaping, deterministic filenames, duplicate GUID detection, and exact round-trip preservation of GUIDs and tags.

## Audio requirements

- Synthesize only `serbian_final`, with no HTML, audio markup, labels, Russian text, or spoken instructions.
- The exported `serbian_final` remains without its optional terminal full stop. If pilot listening shows that the selected voice needs an end-of-sentence prosody cue, the synthesizer may append a full stop to the in-memory TTS request only; do not save it back into the working table or Anki field. Include the exact rendered TTS input in the cache key and manifest.
- Use `language_code="sr-RS"` and one explicitly configured Serbian-capable voice throughout a production run. Discover/verify current voice availability through the provider rather than trusting an old commented constant.
- A reasonable first pilot is the current Gemini TTS model with voice `Leda` and `sr-RS`; Serbian support is preview, so compare it with a non-preview `sr-RS` voice if pronunciation or word omission is unreliable. Do not retain the current `en-US` setting.
- Do not assume every model accepts speaking-rate, SSML, or other controls. Validate configuration against the selected model and keep the accepted settings in the manifest.
- Use deterministic, ASCII-safe, readable, collision-resistant media names consisting of a bounded transliterated sentence slug plus short hashes of the GUID and normalized sentence, for example `sr_sacekaj_me_na_cosku_<guidhash>_<texthash>.mp3`. Do not use either an opaque hash-only name or the unbounded full sentence as a filename.
- Validate each output structurally: the file must exist, be non-empty, and decode successfully as MP3. Manual listening is required for the voice pilot, not for every production batch.
- Interactive playback must be opt-in (`--play`), never the batch default.
- Never regenerate an identical cached clip. Provide `--dry-run`, explicit batch/range selection, and a cost/request estimate before full synthesis.
- Keep exceptional pronunciation prompts in `serbian_tts_overrides.json`, keyed by GUID. These prompts affect synthesis and cache identity but never the visible card text. Preserve each override's reason, `tts-revisit` marker, and review date until a newly generated clip is explicitly accepted without it.

## Anki assembly and safe import

The final TSV must retain these semantics:

```text
#separator:tab
#html:true
#guid column:1
#tags column:4
<GUID>\t<Serbian sentence><br>[sound:<filename>.mp3]\t<Russian translation>\t<original tags>
```

Use the CSV writer for quoting and newlines; do not concatenate TSV strings manually. HTML-escape user text while leaving the intentional `<br>` and `[sound:...]` markup intact.

Before handoff, compare source and output programmatically and report:

- record count and unique GUID count;
- unchanged GUID set and order;
- unchanged tag count and exact tag equality;
- count by change category and review status;
- number of audio files expected, generated, cached, failed, and missing;
- source/output/manifest checksums;
- a small escaped preview of representative records.

Handoff instructions must tell the user to:

1. back up/sync the Anki collection;
2. test a small import against a disposable copy or profile first;
3. copy the generated MP3 files directly into Anki's `collection.media` directory (no subdirectory);
4. import the TSV with HTML allowed and confirm that Anki recognizes the GUID column and reports updates, not thousands of new notes;
5. inspect several updated cards and play their audio before committing to the full import;
6. run Anki's media check afterward to identify now-unused old clips, without deleting them automatically as part of this pipeline.

If a pilot import creates duplicates or changes note count, stop. Do not attempt to clean up the live collection automatically; diagnose field mapping, note type, GUID handling, and import settings first.

## Definition of done

The task is complete only when all 3,658 rows are approved or explicitly listed as unresolved, every included row passes validation, every sound reference resolves to a verified MP3, GUIDs/order/tags match the source exactly, the pilot language and voice were approved, and the user has a reproducible command sequence plus a final verification report. Passing code tests alone is not completion of the linguistic review.

## Current authoritative references

- Anki text import and media rules: <https://docs.ankiweb.net/importing/text-files.html>
- Google Cloud supported voices: <https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types>
- Google Cloud Gemini TTS language/model notes: <https://docs.cloud.google.com/text-to-speech/docs/gemini-tts>
