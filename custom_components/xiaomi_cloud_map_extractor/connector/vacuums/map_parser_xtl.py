"""Parser for XTL (Jonr) vacuum maps: JSON + base64 + LZ4 occupancy grid."""

import base64
import json
import logging
import struct
from typing import Any

from PIL import Image

from vacuum_map_parser_base.config.color import SupportedColor
from vacuum_map_parser_base.map_data import ImageData, MapData, Path, Point, Room
from vacuum_map_parser_base.map_data_parser import MapDataParser

_LOGGER = logging.getLogger(__name__)


def lz4_decompress_block(src: bytes, uncompressed_size: int) -> bytes:
    """Decompress an LZ4 *block* (not frame) in pure Python.

    Home Assistant OS has no C toolchain, so the compiled `lz4` package cannot
    be built there. The map's `map`/`trace` payloads are LZ4 block format; this
    implements decompression only (literal/match sequences), which is all we
    need. Verified byte-for-byte against the `lz4` library on real device data.
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                b = src[i]
                i += 1
                literal_length += b
                if b != 255:
                    break
        out += src[i:i + literal_length]
        i += literal_length
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match_length = (token & 0x0F) + 4  # 4 == LZ4 minimum match
        if (token & 0x0F) == 15:
            while True:
                b = src[i]
                i += 1
                match_length += b
                if b != 255:
                    break
        start = len(out) - offset
        for j in range(match_length):  # byte-by-byte to allow overlap
            out.append(out[start + j])
    if len(out) != uncompressed_size:
        raise ValueError(f"LZ4 block size mismatch: {len(out)} != {uncompressed_size}")
    return bytes(out)


def decode_field0(outer_json: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """Return (field0_meta, occupancy_grid_bytes) for the primary map field."""
    meta = json.loads(base64.b64decode(outer_json["fields"][0]))
    grid = lz4_decompress_block(base64.b64decode(meta["map"]), meta["lz4Len"])
    return meta, grid


# Device headings ("a") are centi-degrees; the parser base library wants degrees.
_CENTIDEGREES_PER_DEGREE = 100

# fields[1] trace records: x int16, y int16, angle int16, flags uint8.
_TRACE_RECORD_SIZE = 7

# Consecutive trace points further apart than this start a new path segment, so a
# gap is not drawn as a straight line across the flat. Real captures show only a
# handful of such jumps (2-43 out of hundreds to thousands of points).
_TRACE_GAP = 5

# Occupancy grid value -> palette color name. 0 is unexplored and 1 is wall; 2 is
# unidentified (small scattered patches, possibly furniture) and is left as floor.
# Everything from 3 up is a room id -- confirmed by fields[3], whose room entries
# carry exactly these numbers in their `room_id`. A real capture used 3,4,5,6,7,8,9
# and 11, so the ids are neither contiguous nor bounded by 8.
#
# An earlier version mapped only 0/1/2, so every room fell through to the
# background: on a real map that silently hid 21239 of 34194 cells (62%), leaving
# bare wall outlines with no floor and no rooms.
_VALUE_COLORS = {
    0: SupportedColor.MAP_OUTSIDE,
    1: SupportedColor.MAP_WALL_V2,
    2: SupportedColor.MAP_INSIDE,
}

# Grid values at or above this are room ids, colored via the palette's room colors
# so the existing GUI "room colors" option applies to them.
_FIRST_ROOM_VALUE = 3


class XtlMapDataParser(MapDataParser):
    """XTL (Jonr) map parser."""

    def unpack_map(self, raw_encoded: bytes, *args, **kwargs) -> bytes:
        # The downloaded artifact is already the plain JSON map; nothing to inflate.
        return raw_encoded

    def parse(self, raw: bytes, *args, **kwargs) -> MapData:
        outer = json.loads(raw)
        meta, grid = decode_field0(outer)
        width, height = meta["width"], meta["height"]
        x_min, y_max = meta["xMin"], meta["yMax"]

        image = self._trim_and_scale(self._build_image(grid, width, height), width, height)

        # Coordinate system for POSITIONS (chargePos, fields[2], the trace), fitted
        # against 842 real trace points:
        #
        #   device x -> image COLUMN:  img_x = x - yMax
        #   device y -> image ROW:     img_y = y - (totalHeight - xMin - height)
        #
        # `yMax` is the column anchor and `xMin` is the window's offset from the
        # canvas BOTTOM, so device y is a top-down row index on the totalHeight-row
        # canvas. Flipping y with (totalHeight - 1 - y) lets the window be anchored
        # at top=xMin and lets ImageDimensions.to_img supply its own y-flip, which
        # together give the formula above.
        #
        # Beware: the ROOM data in fields[3]/fields[9] uses the TRANSPOSED form of
        # this (centerX is the row, centerY the column). Both were verified
        # independently -- the room polygons fit their grid cells to the exact
        # integer on all 16 bounding-box bounds, and the trace fit is below -- so the
        # device really does transpose x/y between rooms and positions;
        # `_parse_rooms` swaps them back into this frame.
        #
        # How this was pinned, after three wrong guesses: fields[1] decodes to
        # 7-byte records (x int16 LE, y int16 LE, angle int16 LE, flags) and its
        # LAST point equals fields[2] exactly in all three captures, which both
        # confirms the record layout and proves the trace shares the position frame.
        # Scoring all 842 points against the occupancy grid, this mapping puts 100%
        # in bounds and 93.6% on room cells; every rival scores ~14% with most points
        # off-canvas.
        #
        # The earlier guesses leaned on the reporter's description of where the dock
        # sits and on a wall obstacle they identified as the dock. That is far weaker:
        # brute-forcing offsets against the grid yields 228 placements that all land
        # on an occupied cell. Two mappings looked plausible that way and both were
        # wrong, and none of the variants is distinguishable on the synthetic fixture,
        # whose window is wide enough to keep every one of them in bounds.
        total_height = meta["totalHeight"]

        def to_library_space(p: Point) -> Point:
            return Point(p.x, (total_height - 1) - p.y, p.a)

        map_data = MapData(0, 1)
        map_data.image = ImageData(
            width * height, x_min, y_max, height, width,
            self._image_config, image, to_library_space,
        )

        map_data.charger = self._parse_position(meta.get("chargePos"))
        map_data.vacuum_position = self._parse_position(self._field(outer, 2))
        self._warn_if_off_canvas(map_data.charger, "charger", map_data.image)
        self._warn_if_off_canvas(map_data.vacuum_position, "vacuum", map_data.image)
        map_data.path = self._decode_path(outer)
        map_data.rooms = self._parse_rooms(outer)
        map_data.vacuum_room = self._room_at(map_data.vacuum_position, meta, grid)

        if map_data.image is not None and not map_data.image.is_empty:
            self._image_generator.draw_map(map_data)
        return map_data

    @staticmethod
    def _warn_if_off_canvas(point: Point | None, name: str, image: ImageData) -> None:
        """Log when a position maps outside the picture instead of vanishing silently.

        PIL clips out-of-range draws without complaint, so a marker whose device
        coordinates fall outside the map window simply never appears -- no error, no
        log line. That is how a wrong coordinate mapping went unnoticed, and it also
        happens legitimately when the device reports a live position outside the
        current map window (seen on a real capture with the robot mid-clean, 23 cells
        beyond the window's y range while the map itself only spanned the rooms).
        """
        if point is None or image is None or image.is_empty:
            return
        p = point.to_img(image.dimensions)
        if not (0 <= p.x < image.data.width and 0 <= p.y < image.data.height):
            _LOGGER.warning(
                "XTL %s position (%s, %s) maps to (%s, %s), outside the %sx%s map "
                "image; it will not be drawn",
                name, point.x, point.y, p.x, p.y, image.data.width, image.data.height,
            )

    @staticmethod
    def _field(outer: dict[str, Any], index: int) -> Any:
        fields = outer.get("fields") or []
        return fields[index] if index < len(fields) else None

    @staticmethod
    def _parse_position(value: Any) -> Point | None:
        """Parse a {"x","y","a"?} position (dict or JSON string) into a Point.

        `a` is a heading in centi-degrees (chargePos reports 18000 for 180.00
        degrees), but the base library treats Point.a as DEGREES: _draw_vacuum
        computes `a / 180 * pi` and _draw_pieslice passes it straight to PIL. Left
        raw, 18000 wraps to 18000 % 360 == 0 and the dock is drawn facing exactly
        backwards; a real heading of 90 degrees arrives as 9000 and is drawn at
        9000 % 360 == 120. So convert to degrees here, at the boundary.
        """
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        if not isinstance(value, dict) or "x" not in value or "y" not in value:
            return None
        angle = value.get("a")
        return Point(value["x"], value["y"], None if angle is None else angle / _CENTIDEGREES_PER_DEGREE)

    def _parse_rooms(self, outer: dict[str, Any]) -> dict[int, Room] | None:
        """Rooms from fields[3] (metadata) and fields[9] (outline polygons).

        Keyed by `room_id`, the value the room's cells carry in the grid and the id
        the robot's start-clean (siid 17, aiid 1, type 3) expects; the per-room `id`
        is only an index. Both fields store points TRANSPOSED relative to positions
        (see `parse`), so every (a, b) becomes position (b, a): on a real capture this
        puts all 351 outline vertices next to a cell of their own room, and each
        room's (centerY, centerX) inside it, while the untransposed reading misses
        85% of them.

        A Room carries only its bounding box, as the base library expects. Must
        never raise: bad room data should cost the rooms, not the map.
        """
        try:
            infos = self._field(outer, 3)
            if isinstance(infos, str):
                infos = json.loads(infos) if infos else []
            outlines = {}
            for item in self._field(outer, 9) or []:
                data = item.get("data") or []
                outlines[item.get("id")] = [(b, a) for a, b in zip(data[::2], data[1::2])]
            rooms: dict[int, Room] = {}
            for info in infos or []:
                room_id = info.get("room_id")
                points = outlines.get(room_id)
                if room_id is None or not points:
                    continue
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                rooms[room_id] = Room(
                    min(xs), min(ys), max(xs), max(ys), room_id,
                    str(info.get("name") or "").strip() or None, info.get("centerY"), info.get("centerX"),
                )
            return rooms or None
        except Exception:
            _LOGGER.debug("XTL rooms decode failed", exc_info=True)
            return None

    @staticmethod
    def _room_at(point: Point | None, meta: dict[str, Any], grid: bytes) -> int | None:
        """Room id of the grid cell under a position, via the mapping in `parse`."""
        if point is None:
            return None
        width, height = meta["width"], meta["height"]
        col = int(point.x) - meta["yMax"]
        row = int(point.y) - (meta["totalHeight"] - meta["xMin"] - height)
        if not (0 <= col < width and 0 <= row < height):
            return None
        value = grid[(height - 1 - row) * width + col]
        return value if value >= _FIRST_ROOM_VALUE else None

    def _decode_path(self, outer: dict[str, Any]) -> Path | None:
        """Decode the vacuum path from fields[1].

        Field 1 is a JSON header {traceId, start, totalCount, trace, lz4Len};
        `trace` is base64 -> LZ4 block -> `lz4Len` bytes, which is a flat array of
        7-byte records:

            x     int16 little-endian
            y     int16 little-endian
            angle int16 little-endian
            flags uint8   (observed 2, 10, 128, 130, 138 -- meaning unknown)

        Confirmed by the last record equalling fields[2] (the live position)
        exactly across three real captures, and by 93.6% of 842 points landing on
        room cells under the position mapping in `parse`.

        `totalCount` is the record count, not the byte count. Records are dropped
        into segments whenever consecutive points jump more than a few cells, so a
        gap in the trace is not drawn as a straight line across the flat.

        Must never raise: a bad trace should cost the path, not the whole map.
        """
        try:
            field = self._field(outer, 1)
            if not field:
                return None
            header = json.loads(base64.b64decode(field))
            if "trace" not in header:
                return None
            raw = lz4_decompress_block(base64.b64decode(header["trace"]), header["lz4Len"])
            if not raw or len(raw) % _TRACE_RECORD_SIZE:
                # Not the record layout we know (the synthetic test fixture is one
                # such case) -- defer rather than emit bogus points.
                return None

            segments: list[list[Point]] = [[]]
            previous: tuple[int, int] | None = None
            for offset in range(0, len(raw), _TRACE_RECORD_SIZE):
                x, y, angle = struct.unpack_from("<hhh", raw, offset)
                if previous is not None and max(abs(x - previous[0]), abs(y - previous[1])) > _TRACE_GAP:
                    segments.append([])
                segments[-1].append(Point(x, y, angle / _CENTIDEGREES_PER_DEGREE))
                previous = (x, y)

            segments = [s for s in segments if len(s) > 1]
            if not segments:
                return None
            return Path(sum(len(s) for s in segments), 1, None, segments)
        except Exception:
            _LOGGER.debug("XTL path decode failed", exc_info=True)
            return None

    def _trim_and_scale(self, img: "Image.Image", width: int, height: int) -> "Image.Image":
        """Crop by the configured trim and resize by the configured scale.

        `ImageDimensions.to_img` multiplies marker coordinates by `scale` and
        offsets them by the trim, but the base library never touches the raster --
        producing it at the matching size is the parser's job. While this was
        missing, any scale other than 1 left the bitmap at 1:1 while markers were
        placed at scale x their true position, so the charger and robot drifted
        right and down in proportion to the scale. Trim had the same mismatch.

        The integer trim arithmetic mirrors `ImageData.__init__` exactly so the
        cropped raster and `dimensions` cannot disagree.
        """
        trim = self._image_config.trim
        left = int(trim.left * width / 100)
        right = int(trim.right * width / 100)
        top = int(trim.top * height / 100)
        bottom = int(trim.bottom * height / 100)
        if left or right or top or bottom:
            # to_img places img_y == 0 at the row `top` pixels below the untrimmed
            # top edge, so the crop is in image space, not device space.
            img = img.crop((left, top, width - right, height - bottom))

        scale = self._image_config.scale
        if scale != 1:
            img = img.resize(
                (int(img.width * scale), int(img.height * scale)), Image.NEAREST
            )
        return img

    def _build_image(self, grid: bytes, width: int, height: int) -> "Image.Image":
        """Render the occupancy grid.

        Grid row 0 carries the lowest device y (yMax), so it is drawn at the
        BOTTOM of the image to match the y-up marker mapping in `parse`. The
        bitmap and the markers must flip together or the charger detaches from
        the walls.
        """
        img = Image.new("RGBA", (width, height))
        px = img.load()
        colors = {v: self._palette.get_color(c) for v, c in _VALUE_COLORS.items()}
        for value in set(grid) - colors.keys():
            colors[value] = (
                self._palette.get_room_color(value)
                if value >= _FIRST_ROOM_VALUE
                else self._palette.get_color(SupportedColor.MAP_OUTSIDE)
            )
        for i, value in enumerate(grid):
            px[i % width, height - 1 - i // width] = colors[value]
        return img
