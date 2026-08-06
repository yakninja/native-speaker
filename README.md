# Native speaker

## Installation

Need Python 3.12 or later and ffmpeg to be installed

- `python3 -m venv .venv`
- `source .venv/bin/activate`
- `pip install -r requirements.txt`

## Running

- `source .venv/bin/activate`
- `python main.py`

## Serbian Anki deck refresh

`main.py` remains the original phrase/TTS reference. The Serbian Anki workflow is a separate, resumable CLI:

```sh
source .venv/bin/activate

# Create or refresh the protected master review table.
python refresh_serbian_deck.py extract

# Export no more than 100 draft rows, edit only the final/change/status/note columns,
# and merge the batch back with protected source columns checked.
python refresh_serbian_deck.py export-batch --output generated/batches/batch-0001.tsv --limit 100
python refresh_serbian_deck.py merge-batch --batch generated/batches/batch-0001.tsv

# Validate while work remains. Omit --allow-incomplete for the final text gate.
python refresh_serbian_deck.py validate --allow-incomplete

# Preview the first paid TTS request set, then run it only after text/voice approval.
python refresh_serbian_deck.py pilot --dry-run
python refresh_serbian_deck.py pilot --yes

# Review synthesized cards in source order. Each card compares the original
# Serbian/Russian note with its replacement, plays the new MP3, and waits for a decision.
python refresh_serbian_deck.py review-audio --start-index 1 --end-index 100

# After all text and audio are complete, assemble and verify the Anki update.
python refresh_serbian_deck.py synthesize --dry-run
python refresh_serbian_deck.py synthesize --yes
python refresh_serbian_deck.py assemble
python refresh_serbian_deck.py verify
```

During audio review, press Enter (or `a`) to accept, `r` to replay, `c` to
record a change request, or `s` to leave the card unchanged and continue.
These controls act immediately without Enter. Use `q` followed by Enter to
save the session and quit. Use `x` followed by Enter, or Ctrl-C, to discard the
session and quit. Reaching the end saves automatically. Accepted cards become
`verified` and disappear from the next run; change requests become
`needs_review` and keep your comment in the review table. Use
`--include-verified` to listen to accepted cards again, or `--list-only` to
preview the queue without playing or changing anything.

Generated review data, MP3s, manifests, and import files are written beneath `generated/` and are ignored by Git. See `AGENTS.md` for the linguistic standard, review gates, and safe Anki import procedure.

## VS Code setup

- Press Ctrl+Shift+P (Cmd+Shift+P on Mac) to open the Command Palette
- Search for "Python: Select Interpreter" and click it.
- Select the interpreter from `.venv/bin/python`.
