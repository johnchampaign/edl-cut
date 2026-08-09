# Licensing scope

The [MIT licence](LICENSE) in this repository covers **the original work here**:

- the code in `edl_cut/`
- the documentation (`README.md`, `FINDINGS.md`, this file)
- `data/aliases.json` — our own nickname table, not part of any upstream dataset
- the scene lists (`*-scenes.yaml`) — scene-boundary decisions, inclusion
  judgments, and labels, all written for this project

It does **not** cover the dataset files vendored into `data/`:

| File | Origin |
|---|---|
| `data/episodes.json` | Jeffrey Lancaster's *Game of Thrones* dataset |
| `data/keyValues.json` | " |
| `data/characters.json` | " |
| `data/locations.json` | " |

Those remain the work of their author. They are redistributed here on the terms
described in [data/ATTRIBUTION.md](data/ATTRIBUTION.md), which asks for citation
of the source repository and a note about what was built with it — both of which
this project provides. **We have no authority to relicense them and have not
attempted to.** If you reuse those files, honour the upstream terms directly
rather than relying on our MIT grant.

## On the source material

This repository contains no audiovisual material from any television series —
no video, no audio, no frames, no thumbnails — and the MIT grant conveys no
rights in any such material.

`edl-cut` emits playback instructions: timestamps pointing into files the user
already owns. The scene lists describe *where* scenes fall and *who appears in
them*; they are not, and cannot be assembled into, a copy of the work. See the
distribution boundary section of [README.md](README.md), which `.gitignore`
enforces.
