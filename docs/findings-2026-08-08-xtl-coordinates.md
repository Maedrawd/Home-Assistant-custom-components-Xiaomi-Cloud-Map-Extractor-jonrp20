# XTL coordinates, grid values and scale, resolved against real hardware (2026-08-08)

Four bugs. All of them were invisible to the test suite, because the committed fixture is
synthetic: its window is wide enough to keep every wrong coordinate variant in bounds, it
contains only grid values 0/1/2, and its trace is not in the real record format.

## Bug 1 — the position coordinate frame

Symptom, in stages: first no charger and no robot anywhere on the map; then both markers
offset; then the robot vanishing again whenever it left the dock.

The real convention for **positions** (`chargePos`, `fields[2]`, the trace):

```
device x -> image COLUMN:  img_x = x - yMax
device y -> image ROW:     img_y = y - (totalHeight - xMin - height)
```

`yMax` is the column anchor; `xMin` is the window's offset from the canvas **bottom**, so
device `y` is a top-down row index on the `totalHeight`-row canvas. Flipping y with
`totalHeight - 1 - y` lets the window be anchored at `top=xMin` and lets
`ImageDimensions.to_img` supply its own y-flip.

**The device transposes x/y between rooms and positions.** The room data in `fields[3]` and
`fields[9]` uses the mirror image of the above (`centerX` is the row, `centerY` the column).
Both were verified independently, so this is a real quirk of the format, not a mistake in
one of the fits. The parser swaps room points back into the position frame when it builds
`map_data.rooms`: on a real capture with four rooms, all 351 outline vertices then sit next to
a cell carrying their own `room_id`, and every `(centerY, centerX)` lands inside its room;
read untransposed, 85% of the vertices miss.

### How it was pinned — after three wrong guesses

`fields[1]` decodes to **7-byte records**: `x` int16 LE, `y` int16 LE, `angle` int16 LE,
`flags` uint8 (observed 2, 10, 128, 130, 138; meaning unknown). Two facts confirm it:

- the **last record equals `fields[2]`** — the live position — exactly, in all three
  captures;
- scoring all **842** trace points from one capture against the occupancy grid, this
  mapping puts **100% in bounds and 93.6% on room cells**. Every rival scores ~14% with
  most points off-canvas.

| mapping | charger | trace on rooms | verdict |
|---|---|---|---|
| `xMin`→x, `yMax`→y (original) | col 174 | — | off-canvas, invisible |
| y→col, x→row mirrored (`83202c1`) | (25, 71) | 13.8% | wrong |
| x→col, y→row mirrored (`5d3bf24`) | (15, 61) | 3.6% | wrong |
| y→col, x→row (`bb2948d`, room frame) | (25, 45) | 14.7% | right for rooms, wrong for positions |
| **x→col, y→row** | **(15, 55)** | **93.6%** | **correct** |

The decisive visual: rendering the decoded path produces a serpentine cleaning trail that
starts at the dock, follows the corridors and fills the room listed in `cleanValues`. That
cannot happen under a wrong mapping.

**Lesson, learned the hard way three times.** Every wrong guess was "confirmed" by weak
evidence: a verbal description of where the dock sits, a wall obstacle identified as the
dock, and finally an exact-looking fit to the *room* data that turned out to be a different
frame from the positions. Brute-forcing offsets against the occupancy grid yields **228**
placements that all land on an occupied cell, so "it lands on floor" and "it lands roughly
right" discriminate almost nothing — and even a perfect fit proves only that you matched
*that* data's frame. What worked was a dense reference set drawn from the same field family
as the thing being mapped, plus a shape check (the path) that a wrong answer could not fake.

## Bug 2 — only 3 of 10 grid values were rendered

`_VALUE_COLORS` mapped values 0, 1 and 2. A real grid used **0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
11**:

| value | count | meaning |
|---|---|---|
| 0 | 11002 | unexplored |
| 1 | 1683 | wall |
| 2 | 270 | unidentified; small scattered patches, left as MAP_INSIDE |
| 3, 4, 5, 6, 7, 8, 9, 11 | 21239 | room ids |

Everything from 3 up fell through to the background colour: **21239 of 34194 cells (62%)**
of a real map. The user saw bare wall outlines with no rooms, which reads as "I see only the
map of my flat".

Value 3 is **not** plain floor — it is a room (`Salon`). `fields[3]` proves it: each room
entry's `room_id` is exactly one of these grid values. The ids are neither contiguous nor
capped at 8, so `_FIRST_ROOM_VALUE` is 3 and anything at or above it is coloured via
`ColorsPalette.get_room_color`. That makes the existing GUI **room colors** option apply to
them for free (it feeds `_overridden_room_colors`, and `get_room_color` wraps ids modulo the
palette size). The reporter confirmed the resulting layout matches their flat, and distinct
colours on the real capture went from 3 to 12.

## Bug 3 — the raster was never scaled or trimmed

`ImageDimensions.to_img` multiplies marker coordinates by `scale` and offsets them by the
trim, but the base library **never touches the raster**; producing it at the matching size
is the parser's job, and `_build_image` returned a plain `width x height` image.

So at any scale other than 1 the bitmap stayed 1:1 while markers were placed `scale x`
further out — the charger and robot drifted right and down in proportion to the scale,
while the rooms looked correct at every scale. Trim had the same mismatch.

`_trim_and_scale` now crops then resizes, mirroring `ImageData.__init__`'s integer trim
arithmetic exactly so the raster and `dimensions` cannot disagree. Verified on the real
capture: at scales 1, 2, 3 and 4.5 the charger pixel is exactly `scale x` its scale-1
position and stays in bounds, and with trim applied the raster size equals the declared
`(dimensions.width * scale, dimensions.height * scale)`.

## Confirmed as a side effect: headings are centi-degrees

Real `chargePos` reports `"a":-17999`. That cannot be degrees, which confirms the
centi-degrees conversion added in f7e0257 (previously only inferred from the synthetic
fixture's `18000`).

## Resolved: "vacuum position stopped showing"

An earlier revision of this document recorded this as an unresolved device quirk — the
live position appearing to fall outside the map window, possibly because the raster lagged
it. That was wrong: it was simply the wrong coordinate frame. Under the corrected mapping
the robot is in bounds in all three captures, including both mid-clean ones.

`_warn_if_off_canvas` stays, because the failure mode it covers is real and was invisible:
PIL clips out-of-range draws silently, so a marker with bad coordinates produced no marker,
no error and no log line. That is how the original mapping bug survived several releases.

## Bug 4 — the path was never decoded

`_decode_path` used to return `None`, on the belief that the trace was "1 byte per point"
in an unknown per-step encoding. It is not: `totalCount` is the **record** count, and each
record is 7 bytes. Real captures decode to 347, 842 and 9701 points.

Points are split into segments whenever consecutive samples jump more than a few cells
(`_TRACE_GAP`), so a gap is not drawn as a straight line across the flat. Real captures show
only a handful of such jumps. The per-point angle is centi-degrees, like every other heading
in this format.

## Still open

- **Grid value 2 is unidentified** (270 cells, small scattered patches). Left as
  MAP_INSIDE. Possibly obstacles or furniture.
- **Marker heading semantics are unverified.** The angle is correctly scaled, but whether
  the swapped anchors also imply a sign or offset change was not tested; it only affects
  which way the pieslice and bin-cover line point.
- **Trace `flags` byte is unidentified** (values 2, 10, 128, 130, 138). Possibly mop/suction state.
- **`rotate` is untested** against a real capture now that the raster is scaled.

## Reproducing

```
python scripts/marker_diagnose.py <diagnostics.json>
```

Walks the failure modes in order and writes the exact PNG the integration serves. Note
diagnostics dumps and their renders contain a floor plan of someone's home — they are
gitignored; keep them out of commits and issue attachments.
