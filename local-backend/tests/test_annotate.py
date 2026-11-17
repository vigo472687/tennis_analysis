"""Annotated evidence images: box styles, unannotated close-up, legend, JPEG."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from tennis_backend.evidence.annotate import (
    BLUE,
    FAR_BOX_PX,
    GREEN,
    LEGEND_EN,
    LEGEND_ZH,
    Overlay,
    annotate_frame,
    ball_box_for,
    contact_sheet,
    encode_jpeg,
    legend_panel,
    render_cell,
    scale_to_height,
    zoom_on_ball,
)


def canvas(width: int = 400, height: int = 300) -> np.ndarray:
    return np.full((height, width, 3), 90, np.uint8)


def count_color(image: np.ndarray, color: tuple[int, int, int]) -> int:
    return int((image == np.array(color, np.uint8)).all(axis=2).sum())


def overlay(**kw) -> Overlay:
    base = dict(
        frame=10,
        time_s=0.33,
        ball=[100.0, 100.0, 110.0, 110.0],
        ball_confidence=0.7,
        racket=[150.0, 80.0, 220.0, 200.0],
        racket_confidence=0.8,
        frames_to_anchor=3,
    )
    base.update(kw)
    return Overlay(**base)


def test_original_is_never_modified() -> None:
    image = canvas()
    before = image.copy()
    annotate_frame(image, overlay(), far=False)
    assert (image == before).all()


def test_ball_is_green_and_racket_is_blue() -> None:
    out = annotate_frame(canvas(), overlay(), far=False)
    assert count_color(out, GREEN) > 0
    assert count_color(out, BLUE) > 0


def test_low_confidence_box_is_dashed_not_solid() -> None:
    solid = annotate_frame(canvas(), overlay(ball_confidence=0.7), far=False)
    dashed = annotate_frame(canvas(), overlay(ball_confidence=0.3), far=False)
    assert 0 < count_color(dashed, GREEN) < count_color(solid, GREEN)


def test_far_view_uses_a_fixed_32px_square_on_the_ball_centre() -> None:
    box = ball_box_for(overlay(ball=[200.0, 150.0, 204.0, 154.0]), far=True)
    assert box is not None
    assert box[2] - box[0] == FAR_BOX_PX and box[3] - box[1] == FAR_BOX_PX
    assert ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) == (202.0, 152.0)


def test_far_view_dashes_every_source_except_detected() -> None:
    solid = annotate_frame(canvas(), overlay(ball_dashed=False), far=True)
    dashed = annotate_frame(canvas(), overlay(ball_dashed=True), far=True)
    assert count_color(dashed, GREEN) < count_color(solid, GREEN)


def test_no_ball_means_no_green_and_no_zoom() -> None:
    o = overlay(ball=None, ball_confidence=None)
    assert count_color(annotate_frame(canvas(), o, far=False), GREEN) == 0
    assert zoom_on_ball(canvas(), o) is None


def test_zoom_is_cut_from_the_unannotated_original() -> None:
    image = canvas()
    cv2.circle(image, (105, 105), 2, (250, 250, 250), -1)  # the "ball"
    zoom = zoom_on_ball(image, overlay())
    assert zoom is not None and zoom.shape[:2] == (160, 160)
    assert count_color(zoom, GREEN) == 0  # no box edge on the ball
    assert zoom[75:85, 75:85].max() >= 250  # ball sits at the centre


def test_zoom_pads_at_the_border() -> None:
    zoom = zoom_on_ball(canvas(), overlay(ball=[0.0, 0.0, 4.0, 4.0]))
    assert zoom is not None and zoom.shape[:2] == (160, 160)


def test_cell_label_carries_frame_time_confidences_and_anchor() -> None:
    plain = canvas()
    out = annotate_frame(plain, overlay(), far=False)
    assert (out[:20] != plain[:20]).any()  # label band drawn on the top rows


def test_render_cell_returns_decodable_jpegs_at_720_high() -> None:
    annotated, zoom = render_cell(
        canvas(1280, 1440), overlay(ball=[600.0, 600.0, 610.0, 610.0]), far=False
    )
    decoded = cv2.imdecode(np.frombuffer(annotated, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[0] == 720
    assert zoom is not None
    assert cv2.imdecode(np.frombuffer(zoom, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (160, 160)


def test_render_cell_without_a_ball_has_no_zoom() -> None:
    _, zoom = render_cell(canvas(), overlay(ball=None, ball_confidence=None), far=False)
    assert zoom is None


def test_legend_states_the_two_cautions_in_both_languages() -> None:
    text_en = " ".join(LEGEND_EN)
    assert "No box does NOT mean" in text_en and "NOT 'this is a hit'" in text_en
    text_zh = " ".join(LEGEND_ZH)
    assert "无框不等于" in text_zh and "不代表这一帧是击球" in text_zh and "请复核" in text_zh


def test_legend_panel_matches_the_requested_width() -> None:
    assert legend_panel(900).shape[1] == 900


def test_contact_sheet_pads_the_last_row_and_adds_the_legend() -> None:
    cells = [canvas(200, 150) for _ in range(5)]
    sheet = contact_sheet(cells, columns=4)
    assert sheet.shape[1] == 800
    assert sheet.shape[0] > 2 * 150  # two rows + legend


def test_scale_to_height_keeps_the_aspect_ratio() -> None:
    assert scale_to_height(canvas(1440, 1280)).shape[:2] == (720, 810)


def test_encode_jpeg_is_smaller_than_raw() -> None:
    image = canvas(720, 1280)
    assert len(encode_jpeg(image)) < image.nbytes // 10


def test_contact_sheet_needs_cells() -> None:
    with pytest.raises(ValueError):
        contact_sheet([])
