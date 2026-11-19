# osu! Beat Sound

日本語: [README.md](README.md)

A metronome for osu!: it plays a beat sound (an NC-style kick, clap, hat, finish, or your own sounds) in time with the music while you play **NoMod**.
It never touches the game itself. It only reads the current song position from [tosu](https://github.com/KotRikD/tosu).

> Unofficial tool, not affiliated with osu! or tosu. Use at your own risk.
> Not recommended for multiplayer, ranked play or any competitive setting (see "Notes").

## Quick start (exe)
1. Download `osu_beat_sound_windows.zip` from **Releases** and **extract it** (running the exe from inside the zip will not work).
2. Start [tosu](https://github.com/KotRikD/tosu) and osu!.
3. Put your sound files in `sounds` and double-click `osu_beat_sound.exe` (no Python needed).
4. When the dot in the top right turns green (connected), play a map. Beats follow the music.

## Running from source
- Windows 10 / 11 and Python 3.10+ (tick "Add python.exe to PATH" when installing).

      python -m pip install -r requirements.txt
      python nc_beat.py

## Sound files
**No sounds are included.** Put your own files in `sounds` using these names (any subset works):

| Role | Example names |
|---|---|
| kick | `kick.wav` / `nightcore-kick.wav` |
| clap | `clap.wav` / `nightcore-clap.wav` |
| hat | `hat.wav` / `nightcore-hat.wav` |
| finish | `finish.wav` / `nightcore-finish.wav` |
| count voice (optional) | `beat1.wav` ... `beat8.wav` (or `count1.wav` / `1.wav`). If present, 1 2 3 4 are spoken on each beat instead of kick / clap |

wav / ogg / mp3 / flac are supported.

## The screen
- **Cover / title**: the background image of the selected map, with title, artist and difficulty.
- **Volume / timing**: sliders in the seek-bar position (negative timing = later, positive = earlier).
- **Center play / pause**: beats on / off.
- **Previous / next**: BPM scale down / up (x0.5, x1, x2). E.g. if a 160 BPM map has notes at 320, use x2.
- **Left button**: time signature (AUTO, 4/4, 3/4, 5/4, 6/4, 7/4).
- **Right "♪"**: which sounds play (kick / clap / hat / finish / count voice), with preview, and "also play in song select".
- **Top-right dot and "•••"**: tosu connection (green = connected, yellow = connecting, red = not connected). "•••" opens Setting (language, theme, accent color, font, always on top, hide while playing, autostart, tosu path ...), connection details, fullscreen (F11) and quit.

Settings are saved to `config.json` when you close the window. The app stays silent when the real NC mod is on, so it never doubles up.

## Odd time signatures
- If the map specifies 3/4, 7/4 etc., the beat follows it automatically.
- If the map says 4/4 but the song is not, pick one with the **left button**.
- Default pattern: beats 1, 3, 5 = kick; 2, 4, 6 = clap; off-beats = hat; bar start = finish. In odd meters the last beat is a clap, so it does not run into the next bar's kick.
- Override per meter in `config.json` (numbers are eighth-note positions in the bar):

      "patterns": { "3": {"kick": [0], "clap": [2, 4], "hat": [1, 3, 5], "finish": [0]} }

## Changing the 4/4 pattern
Edit `pattern_4_4` in `config.json` (with the app closed). One bar = 8 eighth-note steps (0-7). Example: kick on every beat -> `"kick": [0, 2, 4, 6]`.

## Languages
Setting -> Language switches between Japanese and English. To add another, copy `lang_template.json`, translate it, and save it as e.g. `lang/es.json`.

## Building the exe
Double-click `build_exe.bat` to get `dist\osu_beat_sound.exe`. On GitHub, pushing a tag such as `v1.1.0` makes Actions build the zip.

## Troubleshooting
| Symptom | Fix |
|---|---|
| tosu shows "not connected" | Start tosu first |
| Loaded sounds says "none" | Check file names / double extensions (`clap.wav.wav`), then press reload sounds |
| No sound | Check the game state is `play`; try the preview in "♪" |
| Doubled sound | Make sure the app is not running twice |
| Sound is off-beat | Adjust the Timing slider |
| exe is blocked / deleted | Extract the zip first. If it is an antivirus false positive, allow it, or use the Python version |

## Notes
- Unofficial tool, not affiliated with osu! or tosu.
- It does not touch osu! files or memory (it only reads tosu's data) and does not change judgement or score. However, a beat sound can make the song's rhythm easier to follow, so **using it in multiplayer, ranked play, tournaments or events is not recommended**. Follow the organizer's rules.
- Use at your own risk.
