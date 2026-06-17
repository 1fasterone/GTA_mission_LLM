# _UserAssets/Audio

SFX, music, ambient loops, voice lines.

## What goes here

```
Audio/
├── SFX/        gunshots, impacts, footsteps, UI clicks
├── Music/      tracks, stingers, jingles
├── Ambient/    wind, city, water, room tone
└── Voice/      lines, barks, narration
```

## Recommended formats

- **SFX**: WAV (uncompressed) or OGG for shipping. Unity converts on import.
- **Music**: OGG or MP3. Set "Load Type" to **Streaming** in the importer for
  long tracks to avoid memory bloat.
- **Voice**: WAV → Vorbis compression on import.

Set the AudioClip importer's "Force to Mono" on for any 3D-positioned audio
— stereo SFX in 3D space sounds wrong.

## Free sources

- **Freesound.org** — filter by CC0; gigabytes of SFX.
- **OpenGameArt** — both music and SFX, mostly CC0/CC-BY.
- **Free Music Archive** — CC-licensed music.
- **Pixabay Music** — royalty-free.
- **Kenney "Music Loops"** + **"Game Audio"** — CC0, game-ready.
- **YouTube Audio Library** — free for commercial use with attribution.

## Wiring

The generated `Assets/Scripts/AudioManager.cs` looks for AudioCue
ScriptableObjects under `Assets/Audio/Cues/`. Once you've imported your
clips, right-click in `Assets/Audio/Cues/` → Create → Audio → AudioCue, and
drag the clip into the cue. The generated gameplay code references cues
by name, not by clip — so swapping the underlying file doesn't break
anything.

For 3D-spatial audio in micro pipelines: see the generated `Audio` micro's
README for the AudioBank format.
