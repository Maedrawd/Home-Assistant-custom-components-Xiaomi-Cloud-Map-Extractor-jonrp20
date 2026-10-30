import base64
import json
from map_parser_xtl import decode_field0


def test_decode_field0_dimensions(raw_map_bytes):
    outer = json.loads(raw_map_bytes)
    meta, grid = decode_field0(outer)
    assert len(grid) == meta["width"] * meta["height"] == meta["lz4Len"]
    assert set(grid) <= {0, 1, 2}


from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor
from vacuum_map_parser_base.config.drawable import Drawable
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Sizes
from vacuum_map_parser_base.map_data import Point


def _parser(image_config: ImageConfig | None = None):
    from map_parser_xtl import XtlMapDataParser
    return XtlMapDataParser(
        ColorsPalette(), Sizes(), list(Drawable), image_config or ImageConfig(), []
    )


def test_parse_produces_nonempty_image(raw_map_bytes, field0):
    md = _parser().parse(raw_map_bytes)
    assert md.image is not None and not md.image.is_empty
    assert md.image.dimensions.width == field0["width"]
    assert md.image.dimensions.height == field0["height"]


def test_parse_reads_charger_from_json_string_chargepos(raw_map_bytes):
    md = _parser().parse(raw_map_bytes)
    assert md.charger == Point(388, 398, 180.0)


def test_headings_are_converted_from_centidegrees_to_degrees(raw_map_bytes):
    # The device reports heading in centi-degrees: chargePos "a": 18000 is 180.00
    # degrees. The base library treats Point.a as DEGREES -- _draw_vacuum computes
    # a / 180 * pi and _draw_pieslice feeds it straight to PIL -- so passing 18000
    # through raw wrapped to 18000 % 360 == 0 and drew the dock facing exactly
    # backwards. Any real heading was mangled the same way (9000 -> 120 degrees).
    md = _parser().parse(raw_map_bytes)
    assert md.charger.a == 180.0
    assert md.vacuum_position.a == 0.06


def test_build_image_maps_grid_values_to_palette_colors(raw_map_bytes):
    outer = json.loads(raw_map_bytes)
    meta, grid = decode_field0(outer)
    width, height = meta["width"], meta["height"]

    # one grid index for each occupancy value present in the fixture
    samples = {}
    for i, value in enumerate(grid):
        if value not in samples:
            samples[value] = i
    assert set(samples) == {0, 1, 2}

    palette = ColorsPalette()
    expected = {
        0: palette.get_color(SupportedColor.MAP_OUTSIDE),
        1: palette.get_color(SupportedColor.MAP_WALL_V2),
        2: palette.get_color(SupportedColor.MAP_INSIDE),
    }

    # Assert on a freshly built image (no draw_map overlays / rotation) so the
    # value -> color mapping is verified directly on decoded grid data.
    img = _parser()._build_image(grid, width, height)
    px = img.load()
    for value, i in samples.items():
        # palette colors are RGB; PIL stores them in the RGBA image with alpha 255
        want = tuple(expected[value]) + (255,) if len(expected[value]) == 3 else tuple(expected[value])
        # grid row 0 is the BOTTOM of the picture - see _expected_pixel
        assert px[i % width, height - 1 - i // width] == want


def _rgba(color) -> tuple:
    return tuple(color) + (255,) if len(color) == 3 else tuple(color)


def test_build_image_puts_grid_row0_at_image_bottom():
    # Uses a hand-built asymmetric grid on purpose: the committed fixture is a
    # plain rectangle and is perfectly symmetric top-to-bottom (rows == rows[::-1]),
    # so no assertion against it can distinguish a vertically flipped bitmap.
    # Grid row 0 carries the lowest device y (yMax) and must therefore render at
    # the BOTTOM, so the bitmap agrees with the marker mapping in _expected_pixel.
    width, height = 3, 2
    grid = bytes([1, 1, 1, 2, 2, 2])  # row 0 = wall, row 1 = inside
    px = _parser()._build_image(grid, width, height).load()

    palette = ColorsPalette()
    assert px[0, height - 1] == _rgba(palette.get_color(SupportedColor.MAP_WALL_V2))
    assert px[0, 0] == _rgba(palette.get_color(SupportedColor.MAP_INSIDE))


def test_build_image_renders_room_values():
    # 0 unexplored, 1 wall, 2 unidentified, and everything from 3 up is a room id --
    # confirmed by fields[3], whose entries carry these numbers as `room_id`. A real
    # capture used 3,4,5,6,7,8,9,11, so ids are neither contiguous nor capped at 8.
    # Only 0/1/2 used to be mapped, so on a real capture 21239 of 34194 cells (62%)
    # fell through to the background: bare wall outlines, no rooms.
    width, height = 3, 3
    grid = bytes([0, 1, 2, 3, 4, 5, 6, 9, 11])  # row 0 = [0,1,2], row 2 = [6,9,11]
    px = _parser()._build_image(grid, width, height).load()

    palette = ColorsPalette()
    # grid row 0 renders at the bottom, so grid row r -> image row height-1-r
    assert px[0, 2] == _rgba(palette.get_color(SupportedColor.MAP_OUTSIDE))  # value 0
    assert px[1, 2] == _rgba(palette.get_color(SupportedColor.MAP_WALL_V2))  # value 1
    assert px[0, 1] == _rgba(palette.get_room_color(3))  # value 3 IS a room, not floor
    assert px[1, 1] == _rgba(palette.get_room_color(4))
    assert px[1, 0] == _rgba(palette.get_room_color(9))  # non-contiguous id
    assert px[2, 0] == _rgba(palette.get_room_color(11))  # id above 8


def test_parse_reads_vacuum_position_from_field2(raw_map_bytes):
    # fields[2] holds the live vacuum position {"x","y","a","i"} on the real device
    md = _parser().parse(raw_map_bytes)
    assert md.vacuum_position == Point(387, 399, 0.06)


def _expected_pixel(field0: dict, x: int, y: int) -> tuple[int, int]:
    """Where a device point must land in the rendered image.

    Device `x` is the image COLUMN (anchored by yMax) and device `y` the image ROW,
    y being a top-down index on the totalHeight-row canvas with xMin the window's
    offset from the canvas bottom. Fitted against 842 real trace points -- see
    docs/findings-2026-08-08-xtl-coordinates.md. Note the room data in
    fields[3]/fields[9] uses the transposed form of this.
    """
    y_offset = field0["totalHeight"] - field0["xMin"] - field0["height"]
    return x - field0["yMax"], y - y_offset


def test_charger_maps_to_exact_pixel(raw_map_bytes, field0):
    # Asserting the EXACT pixel, not merely "somewhere inside the image". The
    # weaker check this replaced was satisfied by the correct mapping *and* by its
    # vertical mirror (img_y 134 vs 19, summing to height-1), which is why the
    # flipped map in 264f7a0 shipped green.
    md = _parser().parse(raw_map_bytes)
    p = md.charger.to_img(md.image.dimensions)
    assert (p.x, p.y) == _expected_pixel(field0, 388, 398)


def test_vacuum_position_maps_to_exact_pixel(raw_map_bytes, field0):
    md = _parser().parse(raw_map_bytes)
    p = md.vacuum_position.to_img(md.image.dimensions)
    assert (p.x, p.y) == _expected_pixel(field0, 387, 399)


def test_raster_is_scaled_so_markers_stay_aligned(raw_map_bytes, field0):
    # to_img multiplies marker coordinates by scale, but the base library never
    # resizes the raster -- the parser must. While it didn't, any scale other than 1
    # left the bitmap at 1:1 while markers were placed scale x further out, so the
    # charger and robot drifted right and down in proportion to the scale.
    scale = 2
    md = _parser(ImageConfig(scale=scale)).parse(raw_map_bytes)
    assert md.image.data.size == (field0["width"] * scale, field0["height"] * scale)

    p = md.charger.to_img(md.image.dimensions)
    want_x, want_y = _expected_pixel(field0, 388, 398)
    assert (p.x, p.y) == (want_x * scale, want_y * scale)
    # and the marker must still be inside the scaled raster
    assert 0 <= p.x < md.image.data.width and 0 <= p.y < md.image.data.height


def test_markers_land_inside_the_image(raw_map_bytes, field0):
    md = _parser().parse(raw_map_bytes)
    for point in (md.charger, md.vacuum_position):
        p = point.to_img(md.image.dimensions)
        assert 0 <= p.x < field0["width"]
        assert 0 <= p.y < field0["height"]


def _trace_field(points: list[tuple[int, int, int]], flags: int = 128) -> str:
    """Build a fields[1] payload: 7-byte records, LZ4-block-compressed, base64'd.

    Emitted as LZ4 "stored" blocks (token 0xF0 + length bytes, no match) so the
    parser's real decompressor is exercised rather than bypassed.
    """
    import struct as _struct

    body = b"".join(_struct.pack("<hhhB", x, y, a, flags) for x, y, a in points)
    out = bytearray()
    literal = len(body)
    if literal < 15:
        out.append(literal << 4)
    else:
        out.append(0xF0)
        remaining = literal - 15
        while remaining >= 255:
            out.append(255)
            remaining -= 255
        out.append(remaining)
    out += body
    return base64.b64encode(
        json.dumps({"traceId": 1, "start": 1, "totalCount": len(points),
                    "trace": base64.b64encode(bytes(out)).decode(), "lz4Len": len(body)}).encode()
    ).decode()


def test_path_decoded_from_seven_byte_trace_records(raw_map_bytes, field0):
    # fields[1] holds 7-byte records (x/y/angle int16 LE + flags). Confirmed on real
    # captures by the last record equalling fields[2] exactly.
    outer = json.loads(raw_map_bytes)
    pts = [(390, 400, 0), (391, 401, 100), (392, 402, 200)]
    outer["fields"][1] = _trace_field(pts)
    md = _parser().parse(json.dumps(outer).encode())

    assert md.path is not None
    assert [(p.x, p.y) for p in md.path.path[0]] == [(x, y) for x, y, _ in pts]
    assert md.path.path[0][1].a == 1.0  # angle is centi-degrees -> degrees
    # and the points must map into the image like any other position
    p = md.path.path[0][0].to_img(md.image.dimensions)
    assert (p.x, p.y) == _expected_pixel(field0, 390, 400)


def test_path_splits_into_segments_on_a_jump(raw_map_bytes):
    # A gap must not be drawn as a straight line across the flat.
    outer = json.loads(raw_map_bytes)
    outer["fields"][1] = _trace_field(
        [(390, 400, 0), (391, 401, 0), (450, 470, 0), (451, 471, 0)]
    )
    md = _parser().parse(json.dumps(outer).encode())
    assert md.path is not None and len(md.path.path) == 2
    assert [(p.x, p.y) for p in md.path.path[1]] == [(450, 470), (451, 471)]


def test_path_deferred_when_field1_present(raw_map_bytes):
    # fields[1] is present on the real device (JSON header with a "trace" blob),
    # but its 1-byte-per-point encoding is not yet reverse-engineered, so path
    # decoding is intentionally deferred to None (never raising).
    outer = json.loads(raw_map_bytes)
    assert outer["fields"][1]  # guard: fixture really does carry a path field
    md = _parser().parse(raw_map_bytes)
    assert md.path is None


def _stored_lz4(body: bytes) -> bytes:
    """LZ4 block holding `body` as a single literal run (see `_trace_field`)."""
    out = bytearray()
    if len(body) < 15:
        out.append(len(body) << 4)
    else:
        out.append(0xF0)
        remaining = len(body) - 15
        while remaining >= 255:
            out.append(255)
            remaining -= 255
        out.append(remaining)
    return bytes(out + body)


def _with_rooms(raw_map_bytes: bytes) -> dict:
    # fields[3]: room metadata, fields[9]: outline polygons keyed by room_id. Both
    # store points transposed relative to positions: (a, b) is position (b, a).
    outer = json.loads(raw_map_bytes)
    outer["fields"][3] = json.dumps([
        {"id": 1, "name": "Kitchen  ", "type": "4", "centerX": 330, "centerY": 420, "room_id": 7},
        {"id": 2, "name": "", "type": "", "centerX": 400, "centerY": 390, "room_id": 3},
        {"id": 3, "name": "No outline", "centerX": 1, "centerY": 1, "room_id": 9},
    ])
    outer["fields"] += [""] * (10 - len(outer["fields"]))
    outer["fields"][9] = [
        {"id": 7, "data": [303, 397, 303, 444, 360, 444, 360, 397]},
        {"id": 3, "data": [380, 380, 380, 400, 420, 400, 420, 380]},
    ]
    return outer


def test_rooms_parsed_from_fields_3_and_9_in_position_frame(raw_map_bytes):
    md = _parser().parse(json.dumps(_with_rooms(raw_map_bytes)).encode())

    assert set(md.rooms) == {7, 3}  # keyed by room_id; a room without an outline is skipped
    kitchen = md.rooms[7]
    assert (kitchen.x0, kitchen.y0, kitchen.x1, kitchen.y1) == (397, 303, 444, 360)
    assert kitchen.number == 7
    assert kitchen.name == "Kitchen"  # the app pads some names with spaces
    assert (kitchen.pos_x, kitchen.pos_y) == (420, 330)
    assert md.rooms[3].name is None  # unnamed: nothing for ROOM_NAMES to draw


def test_rooms_absent_or_malformed_cost_only_the_rooms(raw_map_bytes):
    md = _parser().parse(raw_map_bytes)  # fixture: fields[3] empty, no fields[9]
    assert md.rooms is None

    outer = _with_rooms(raw_map_bytes)
    outer["fields"][3] = "{not json"
    md = _parser().parse(json.dumps(outer).encode())
    assert md.rooms is None
    assert md.image is not None and not md.image.is_empty


def test_vacuum_room_read_from_grid_under_the_robot(raw_map_bytes, field0):
    outer = json.loads(raw_map_bytes)
    assert _parser().parse(raw_map_bytes).vacuum_room is None  # fixture has no room cells

    width, height = field0["width"], field0["height"]
    grid = bytearray([2]) * (width * height)
    col, row = _expected_pixel(field0, 387, 399)  # fields[2], the robot
    grid[(height - 1 - row) * width + col] = 5
    meta = dict(field0, map=base64.b64encode(_stored_lz4(bytes(grid))).decode(), lz4Len=len(grid))
    outer["fields"][0] = base64.b64encode(json.dumps(meta).encode()).decode()

    assert _parser().parse(json.dumps(outer).encode()).vacuum_room == 5
