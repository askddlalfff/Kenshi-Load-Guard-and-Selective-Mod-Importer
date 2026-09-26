# Kenshi Load Guard & Selective Mod Importer

Two standalone, standard-library-only Python tools for **Kenshi** (Windows, Python 3.10+):

| File | What it does |
|------|--------------|
| `kenshi_load_guard.py` | Prevents crashes while **loading a save and during gameplay** — especially the infamous crash when a heavily-modded game suddenly loads many character animations at once. |
| `kenshi_mod_importer.py` | **Imports items, buildings, characters, races, hairstyles and more from someone else's mod into your own mod** — a job the official FCS editor makes painful — and outputs one clean, self-contained merged mod. |

No third-party dependencies. No installer. No changes to your mods or savegames.

---

## 1. kenshi_load_guard.py — Kenshi Load Guard

### Why it exists

With large mod lists, Kenshi tends to crash at two moments: while **loading a save**, and when the game suddenly **streams in many characters at once** (entering a busy town, a big raid, etc.). The usual suspect is a burst of parallel resource/animation work hitting the engine at the same instant. This tool attacks that exact moment.

&gt; The author tested it on animation-burst crashes with heavy mod lists where **RE_Kenshi** did not help. Your mileage may vary — but it never touches anything it can't undo.

### How it works — three independent layers

**1. CPU-core limiting (the main fix)**
- Launches `kenshi_x64.exe` in a **suspended state** (Windows `CreateProcessW` + `CREATE_SUSPENDED`), applies `SetProcessAffinityMask` to restrict the game to 1–8 logical cores (default: **2**), then resumes the main thread.
- Lower instantaneous parallelism = fewer simultaneous animation/resource jobs = no burst crash. Once your save is loaded and the scene is stable, click **Restore all cores** to get full performance back.
- Already running the game via Steam or RE_Kenshi? Click **Attach to running game** and toggle protection on demand.
- If anything fails while setting up protection, the suspended process is terminated immediately — it never leaves a half-configured game running.

**2. Light-load preset (optional, reversible)**
- Temporarily lowers only the `settings.cfg` values that stress loading: population multiplier, squad size, raid size, NPC range, object view range, view distance.
- A byte-exact backup is kept first; on game exit the tool restores **only the keys it changed**, and refuses to overwrite values you (or the game) modified in the meantime.
- Works with Steam / RE_Kenshi too: apply the preset, start the game your usual way, then attach.

**3. Automatic crash/load capture**
- Watches `kenshi.log`, `kenshi_info.log`, etc. When the game exits, it builds a **paste-ready diagnostic report** and copies it to your clipboard automatically:
  - Exit code of the process
  - Full enabled-mod list (original `mods.cfg` order preserved)
  - Whether the KCF animation-crash patch (`_kenshi_fix1.asi`) is present
  - Missing `.skeleton` / `.mesh` detection with occurrence counts and surrounding log context
  - The true tail of each log (up to 180 lines)
  - Where each missing asset actually lives on disk (searched across `mods/`, `data/`, and the Steam Workshop folder, 15-second budget)
  - Any `crashDump` zips, timestamped against your session

### Usage

```bat
python kenshi_load_guard.py
```

1. Point it at your Kenshi folder (auto-detected for Steam installs).
2. **Diagnose a crash:** click *Capture & Launch* (no core limiting, one variable less) → reproduce the problem → the report is saved and copied automatically when the game exits.
3. **Play protected:** click *Protected Launch* (limits cores), or *Light-load Launch* (preset + core limit), or attach to a running game and hit *Enable Load Guard* before loading a save.

### Safety guarantees

- Never edits mods, `mods.cfg` order, or save files.
- The only file it ever writes is `settings.cfg`, and only reversibly, with a backup.
- Report generation is strictly read-only.

---

## 2. kenshi_mod_importer.py — Kenshi Selective Mod Importer

### Why it exists

FCS is a great editor for making mods from scratch, but **merging content from an existing mod into your own mod** (one item, one building, one race — not the whole thing) is notoriously awkward. This tool does exactly that, record by record, with dependency tracking and automatic asset handling.

### Features

- **Standalone FCS `.mod` parser** (format types **16 and 17**, header metadata preserved byte-for-byte). Every record is validated with a lossless round-trip check — if anything can't be parsed perfectly, the tool stops instead of writing a corrupt mod.
- **Selective import GUI**: open the *source* mod and *your* mod, double-click to tick records (items, weapons, armor, buildings, characters, races, hairstyles/attachments, animations...), with live search and category filtering.
- **Recursive dependency resolution** by String ID: whatever your selected records reference (attachments, instances, string/file fields) is pulled in automatically.
- **Correct merge semantics**: source records that *override* your records are merged field-by-field, key-by-key; genuinely new records are appended in full; ID collisions and incompatible record types are rejected with a clear error.
- **Automatic asset handling**: file-path fields are rewritten to point inside the generated mod, referenced assets (`.mesh`, `.skeleton`, `.dds`, …) are copied along, with an optional *copy all source assets* mode for maximum completeness. Conflicting asset paths fail closed rather than silently overwriting.
- **Hair binding**: optionally attach the imported hairstyles to a race in your mod in one click.
- **Your originals are never touched.** Output is a brand-new folder directly under `Kenshi\mods` (e.g. `myMod_merged`), written atomically (temp dir → verify by re-parsing → rename), plus an **`import_report.json`** listing every imported record and every file that needs a manual eye.
- **CLI validation mode**: `python kenshi_mod_importer.py --check some.mod` verifies that a mod parses losslessly.

### Usage

```bat
python kenshi_mod_importer.py
```

1. Open the **source** `.mod` (the mod you want content from) and **your** `.mod`.
2. Double-click records to select them; dependencies follow automatically.
3. (Optional) pick a race to receive the imported hairstyles.
4. Click *Import* → get `Kenshi\mods\&lt;yourMod&gt;_merged\&lt;yourMod&gt;_merged.mod`.
5. In your game launcher, **enable the merged mod and disable the original** — never load both at once.

Then open the result in FCS and check things in-game (building actions, character appearance), especially the files listed in `import_report.json`.

### Limitations (read this)

- Materials *inside* mesh files, skeleton links, and dependencies on third-party mods cannot always be inferred from the `.mod` alone — the report lists every suspicious reference for manual review.
- Import only from mods whose authors permit reuse, and **credit them** in your mod description.

### Format references

Format work was cross-checked against **Weaver** (Steam 797652627), **Superfly-Johnson/kenshi-mod-tools** and **LucasSKrewer/kenshi-modkit**. No source code from those projects is included.

---

## Help wanted: `.exe` packaging

Both tools are currently **Python-only**. If you'd like to contribute a PyInstaller (or similar) build — ideally CI-published via GitHub Actions — pull requests are very welcome.

## Requirements

- Windows 10/11
- Python 3.10+ (tkinter included in the standard Windows installer)

## Disclaimer

Unofficial fan tools, not affiliated with or endorsed by Lo-Fi Games. *Kenshi* is a trademark of Lo-Fi Games. Use at your own risk; both tools are designed to be strictly reversible, but backups are always a good idea.

