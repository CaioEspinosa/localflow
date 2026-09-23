# LocalFlow

LocalFlow is voice dictation for Windows that runs entirely on your own PC. You press a shortcut, talk, and the text appears wherever your cursor is. There is no subscription, and nothing you say leaves the computer.

Under the hood, faster-whisper turns your speech into text, a local Ollama model removes filler words and fixes the punctuation, and LocalFlow pastes the result into the app you were typing in. It can also read selected text out loud with Piper.

## Install

You need Windows 10 or 11 and Python 3.11 or newer (tested with 3.14). It uses an NVIDIA GPU when there is one and falls back to the CPU otherwise.

1. Create the environment and install the dependencies from the project folder.
   ```
   py -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   On the first start the app downloads the speech model, faster-whisper `large-v3-turbo`, which is about 1.6 GB.
2. For text cleanup, install [Ollama](https://ollama.com) and run `ollama pull gemma3:4b`. This step is optional. Without Ollama you get the raw transcript.
3. For read-aloud, extract [Piper for Windows](https://github.com/rhasspy/piper/releases) so that `piper\piper\piper.exe` exists, and put the voices `pt_BR-faber-medium` and `en_US-lessac-medium` (the `.onnx` and `.onnx.json` files from [piper-voices](https://huggingface.co/rhasspy/piper-voices)) in `piper\voices\`. Also optional. The Piper files stay out of the repo because they take about 160 MB, and without them read-aloud uses the Windows voice.

## Using it

Double-click `LocalFlow.vbs` to start it in the background. No window opens, only an icon next to the clock, and right-clicking that icon gives you *Sair* to quit. Run `iniciar-com-windows.bat` once if you want LocalFlow to start with Windows, and `remover-inicio-automatico.bat` to undo it. When something goes wrong, `run.bat` starts it with a console so you can read the logs.

Once the models load, which takes a few seconds, dictation works in any app.

1. Press `Ctrl+Win` and talk. A small pill with a live waveform shows up at the bottom of the screen.
2. Stop talking for about 1.5 seconds, or press `Ctrl+Win` again. LocalFlow pastes the cleaned-up text at your cursor.
3. If you need to think, click the red button at the right end of the pill. The mic pauses, the icon turns into a play sign and the wave goes flat. Click it again to carry on. LocalFlow discards anything you say during the pause, and pressing `Ctrl+Win` while paused pastes what you said before it.
4. To give up, click the X at the left end of the pill or press Esc. LocalFlow then pastes nothing.

Clicking the pill's buttons never takes focus away from the app you are typing in, and when you are not recording, clicks go straight through the pill to whatever is under it. Esc does reach the app you are in as well. If Esc closes a dialog or clears a field there, set `"esc_cancels": false`.

You also hear what is going on. A rising chime means it is listening and a falling one means the text went in. A low tone means it heard nothing, or that you cancelled.

Long dictations work too. After a few seconds of talking, the silence LocalFlow waits for grows a little, so a pause to think doesn't cut you off, and one recording can last three minutes without counting the time paused. Ollama cleans long transcripts in pieces, because on a single long request the model tends to skip whole sentences.

If you prefer push-to-talk, set `"activation": "hold"`. You then hold the shortcut while you talk and let go to finish, and the pill has no buttons.

## Read aloud

Select text in any app and press `Ctrl+Alt+S`. A teal wave appears and Piper reads the text with a Portuguese or an English voice, whichever matches the text. The same keys stop it. With nothing selected, it reads the last thing you copied.

## Languages

For each recording, dictation decides between Portuguese and English. Limiting the choice to those two works much better than open detection on short clips. To change it, set `whisper.language` to `["pt", "en"]`, to a single language such as `"pt"`, or to `"auto"`.

## Configuration

Every setting lives in `config.json`.

| Key | Default | What it does |
|---|---|---|
| `hotkey` | `["ctrl", "win"]` | Dictation shortcut. Accepts ctrl, shift, alt, win, capslock, space, f1 to f12, a to z and 0 to 9 |
| `activation` | `"toggle"` | `"toggle"` means press once and silence ends it. `"hold"` means hold the keys while you talk |
| `pill_buttons` | `true` | Shows the X and the pause button on the pill (toggle mode only) |
| `esc_cancels` | `true` | Lets Esc cancel a recording (toggle mode only) |
| `auto_stop.silence_ms` | `1500` | Silence, in ms, that ends a dictation |
| `auto_stop.silence_ms_long` | `2600` | The same silence in a long dictation |
| `auto_stop.long_after_sec` | `12` | When the wait reaches `silence_ms_long`. It starts growing at half this time |
| `auto_stop.min_level` | `0.008` | Mic level that counts as speech. Lower it for a quiet mic |
| `auto_stop.start_timeout_sec` | `8` | Gives up if it hears nothing for this long after the shortcut |
| `auto_stop.max_sec` | `180` | Longest dictation in seconds, pauses not included |
| `preroll_ms` | `600` | Audio kept from just before you press the shortcut |
| `tail_ms` | `300` | In hold mode, how long it keeps recording after you let go |
| `min_speech_sec` | `0.3` | LocalFlow ignores recordings shorter than this |
| `inject_mode` | `"paste"` | `"paste"` uses the clipboard and Ctrl+V. `"type"` simulates keystrokes |
| `sounds`, `sound_volume` | `true`, `0.15` | The chimes and their volume |
| `whisper.model` | `"large-v3-turbo"` | Any faster-whisper model. `"small"` is faster and makes more mistakes |
| `whisper.device` | `"auto"` | `"auto"` picks the GPU when there is one. `"cuda"` or `"cpu"` force it |
| `whisper.language` | `["pt", "en"]` | See Languages |
| `whisper.beam_size` | `1` | Higher values are slower and slightly more accurate |
| `ollama.enabled` | `true` | `false` skips the cleanup |
| `ollama.url` | `"http://localhost:11434"` | Where Ollama runs. LocalFlow talks to `localhost` through 127.0.0.1, which avoids a two-second IPv6 delay on Windows |
| `ollama.model` | `"gemma3:4b"` | Any Ollama model. gemma3:4b paraphrased Portuguese the least in testing |
| `ollama.timeout_sec` | `15` | Time limit for each request to Ollama |
| `ollama.chunk_chars` | `600` | Size of the pieces LocalFlow splits a long transcript into for cleanup |
| `ollama.budget_sec` | `40` | Total time for cleanup. Past it, LocalFlow pastes the remaining pieces as spoken |
| `read_aloud.enabled` | `true` | Turns the read-aloud shortcut on or off |
| `read_aloud.hotkey` | `["ctrl", "alt", "s"]` | Read-aloud shortcut |
| `read_aloud.speed` | `1.0` | Reading speed. `1.2` is 20% faster |
| `read_aloud.voices` | pt and en | Piper voice for each language, from `piper\voices\` |

## Privacy

Audio, transcription, cleanup and speech all run on your machine. The app does write the text of every dictation to `localflow.log`, next to `app.py`, to make debugging possible. Git ignores that file, and you can delete it at any time.

## Tests

```
.venv\Scripts\python.exe tests\run_all.py
```

The tests run offline. They need neither the microphone nor Ollama nor the speech model, and they never press real keys or click on anything. The pill tests flash a small window at the bottom of the screen for about a second.

## How it works

Everything is in `app.py`.

- `Recorder` keeps the microphone open at the device's own sample rate and holds on to the last 600 ms, so a word spoken right as you press the shortcut still makes it in. Pausing stops it from keeping audio.
- Every 15 ms the main loop reads the shortcut, Esc and the read-aloud keys with `GetAsyncKeyState`.
- `Overlay` draws the pill as a layered window with per-pixel transparency. Its window procedure answers clicks without ever activating the pill, which is what keeps the focus on your app.
- faster-whisper transcribes with its built-in voice activity detection, using float16 on CUDA.
- `OllamaCleaner` sends the transcript to Ollama in pieces and checks each answer. When the model drops words or translates, it keeps the raw text for that piece.
- `Speaker` streams Piper's audio straight to the sound card.
- `inject()` saves the clipboard, pastes with a simulated Ctrl+V and then restores what you had copied.

## License

LocalFlow's code is under the [MIT license](LICENSE). You can use it, change it and share it, in commercial projects too, as long as you keep the copyright notice.

The software and models it downloads are not part of this repository and keep their own licenses. faster-whisper, the Whisper `large-v3-turbo` model, Ollama and Piper are MIT. Gemma 3 comes under the Gemma Terms of Use, and each Piper voice lists its own license on its model card. Most of the Python packages in `requirements.txt` are MIT, BSD or Apache 2.0, while pystray is LGPL 3.0 and NVIDIA's CUDA libraries are proprietary.

LocalFlow is an independent project. It has no affiliation with Wispr Flow or the company behind it, and it contains none of their code or assets. Wispr Flow is a trademark of its owner.
