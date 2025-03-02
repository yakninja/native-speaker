# Installation

Need conda and ffmpeg to be installed

# Running

- `conda activate native-speaker`
- `conda env update --name native-speaker --file environment.yml`
- `python main.py`

# VS Code setup

- `conda info --envs` to list env paths
- Press Ctrl+Shift+P (Cmd+Shift+P on Mac) to open the Command Palette
- Search for "Python: Select Interpreter" and click it.
- If your Conda environment appears in the list, select it.
- If it doesn't appear, click "Enter interpreter path" → "Find" and navigate to:
  `/path/to/conda/envs/native-speaker/bin/python`