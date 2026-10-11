from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime
from numbers import Number
from pathlib import Path
from typing import Iterable

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches

TAG_KPI = re.compile(r"\{\{\s*kpi:([a-zA-Z0-9_\-]+)\s*\}\}")
TAG_CHART = re.compile(r"\{\{\s*chart:([a-zA-Z0-9_\-]+)\s*\}\}")

# Layout rules live in config/deck_layout.json (override with DECK_LAYOUT_PATH):
#   manual_slides        slides maintained by hand — every shape on them is preserved untouched
#   manual_chart_keys    chart keys whose pictures are never replaced
#   chart_box_overrides  per-chart [left, top, width, height] (inches) when a chart needs a wider box
#                        than its tagged placeholder
_LAYOUT_PATH = Path(os.environ.get("DECK_LAYOUT_PATH") or Path(__file__).parent / "config" / "deck_layout.json")
try:
    _LAYOUT = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:
    _LAYOUT = {}

MANUAL_CHART_KEYS: set[str] = set(_LAYOUT.get("manual_chart_keys", []))
MANUAL_SLIDE_NUMBERS: set[int] = set(_LAYOUT.get("manual_slides", []))
CHART_BOX_OVERRIDES: dict[str, tuple[float, float, float, float]] = {
    key: tuple(box) for key, box in _LAYOUT.get("chart_box_overrides", {}).items()
}


def format_value(value: object, fmt: str = "auto") -> str:
    if value is None:
        return ""
    if fmt == "text":
        return str(value)
    if fmt == "percent":
        if isinstance(value, Number):
            text = f"{float(value):.2f}".rstrip("0").rstrip(".")
            return f"{text}%"
        return str(value)
    if fmt == "compact":
        if isinstance(value, Number):
            num = float(value)
            if abs(num) >= 1000:
                text = f"{num / 1000:.2f}".rstrip("0").rstrip(".")
                return f"{text}K"
            return f"{num:,.0f}"
        return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}" if not value.is_integer() else f"{int(value):,}"
    return str(value)


def walk_shapes(shapes) -> Iterable:
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from walk_shapes(shape.shapes)


def shape_text(shape) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    return "\n".join(paragraph.text for paragraph in shape.text_frame.paragraphs)


def set_shape_text(shape, text: str) -> None:
    if not getattr(shape, "has_text_frame", False):
        return
    text_frame = shape.text_frame
    # Keep the paragraph chosen by the template designer. Some card shapes deliberately use
    # leading blank paragraphs to position a caption below the chart; moving replacement text to
    # paragraph zero makes that caption overlap the card heading and chart.
    populated = [
        (p_idx, r_idx)
        for p_idx, paragraph in enumerate(text_frame.paragraphs)
        for r_idx, run in enumerate(paragraph.runs)
        if run.text.strip()
    ]
    if populated:
        target_p, target_r = populated[-1]
        for p_idx, paragraph in enumerate(text_frame.paragraphs):
            for r_idx, run in enumerate(paragraph.runs):
                run.text = text if (p_idx, r_idx) == (target_p, target_r) else ""
    else:
        text_frame.text = text


def get_alt_text(shape) -> str:
    try:
        c_nv_pr = shape._element.xpath(".//p:cNvPr")[0]
        return c_nv_pr.get("descr") or ""
    except Exception:
        return ""


def detect_tag(shape, prefix: str) -> str | None:
    candidates = [shape.name or "", get_alt_text(shape), shape_text(shape)]
    for candidate in candidates:
        if f"{prefix}:" not in candidate:
            continue
        match = re.search(rf"{prefix}:([a-zA-Z0-9_\-]+)", candidate)
        if match:
            return match.group(1)
    return None


def replace_shape_with_picture(
    slide, shape, image_path: Path, chart_key: str | None = None
) -> None:
    """Replace a tagged placeholder and fill its exact PowerPoint card bounds.

    Dashboard capture already isolates the chart renderer (without the duplicate Lakeview title
    band). The slide's card is the layout authority, so use its full width and height instead of
    shrinking a wide dashboard canvas into a small strip surrounded by white space.
    """
    override = CHART_BOX_OVERRIDES.get(chart_key or "")
    if override:
        box_left, box_top, box_width, box_height = (Inches(value) for value in override)
    else:
        box_left, box_top = shape.left, shape.top
        box_width, box_height = shape.width, shape.height
    shape_name = shape.name
    shape_alt = get_alt_text(shape)
    shape._element.getparent().remove(shape._element)

    # Contain proportionally with a small inset: use as much of the card as possible without
    # distorting labels, bars, maps, or text. Capture-time trimming removes the large dashboard
    # margins first, so this no longer collapses a chart into a narrow strip.
    inset = int(min(box_width, box_height) * 0.025)
    avail_width = max(1, box_width - 2 * inset)
    avail_height = max(1, box_height - 2 * inset)
    picture = slide.shapes.add_picture(str(image_path), box_left, box_top)
    img_width, img_height = picture.width, picture.height
    if img_width and img_height:
        scale = min(avail_width / img_width, avail_height / img_height)
        picture.width = int(img_width * scale)
        picture.height = int(img_height * scale)
        picture.left = box_left + (box_width - picture.width) // 2
        picture.top = box_top + (box_height - picture.height) // 2
    # Keep the semantic tag on the new picture. Without this, a deck generated in July could not be
    # used as August's input because the first refresh silently renamed `chart:key` to `Picture 37`.
    picture.name = shape_name
    try:
        c_nv_pr = picture._element.xpath(".//p:cNvPr")[0]
        c_nv_pr.set("descr", shape_alt or shape_name)
    except Exception:
        pass


def build_month_replacements(template_month: str, source_month: str) -> dict[str, str]:
    """Return numeric and human-readable month replacements for an untagged legacy deck."""
    try:
        old = datetime.strptime(template_month.replace("-", "."), "%Y.%m")
        new = datetime.strptime(source_month.replace("-", "."), "%Y.%m")
    except (TypeError, ValueError):
        return {template_month: source_month} if template_month and source_month else {}
    return {
        old.strftime("%Y.%m"): new.strftime("%Y.%m"),
        old.strftime("%Y-%m"): new.strftime("%Y-%m"),
        old.strftime("%B %Y"): new.strftime("%B %Y"),
    }


def apply_text_replacements(shape, replacements: dict[str, str]) -> int:
    """Replace literal substrings in a shape's text, run by run, preserving formatting.

    Used to roll month labels forward, e.g. "Current data until 2026.06" -> "... 2026.07",
    without touching the rest of the text box. Returns the number of runs changed.
    """
    if not replacements or not getattr(shape, "has_text_frame", False):
        return 0
    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            new_text = run.text
            for old, new in replacements.items():
                if old in new_text:
                    new_text = new_text.replace(old, new)
            if new_text != run.text:
                run.text = new_text
                changed += 1
    return changed


def refresh_powerpoint(
    file_bytes: bytes,
    scalar_config: list[dict],
    scalar_values: dict[str, object],
    chart_images: dict[str, Path],
    link_kpi_fallbacks: dict[str, str] | None = None,
    apply_link_fallbacks: bool = False,
    month_replacements: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, int]]:
    presentation = Presentation(io.BytesIO(file_bytes))
    scalar_format = {item["key"]: item.get("format", "auto") for item in scalar_config}
    # The link-fallback feature rewrites the TEXT of any run hyperlinked to a known dashboard
    # into a KPI number. That destroys navigation links (e.g. the "Links for Internal
    # Databricks Sources" slide), so it is OFF by default and must be explicitly enabled.
    link_kpi_fallbacks = link_kpi_fallbacks if (apply_link_fallbacks and link_kpi_fallbacks) else {}
    month_replacements = month_replacements or {}

    stats = {"kpi_updates": 0, "chart_updates": 0, "linked_text_updates": 0, "marker_updates": 0, "month_updates": 0}

    for slide_number, slide in enumerate(presentation.slides, start=1):
        if slide_number in MANUAL_SLIDE_NUMBERS:
            continue
        # Some templates tag two shapes on one slide with the SAME chart key (a tagging
        # error). Replace only the first per key per slide, so we don't duplicate the image.
        used_chart_keys: set[str] = set()
        # Copy list, because replacing charts changes slide.shapes while iterating.
        for shape in list(walk_shapes(slide.shapes)):
            chart_key = detect_tag(shape, "chart")
            if (chart_key and chart_key not in MANUAL_CHART_KEYS
                    and chart_key in chart_images and chart_key not in used_chart_keys):
                replace_shape_with_picture(slide, shape, chart_images[chart_key], chart_key)
                used_chart_keys.add(chart_key)
                stats["chart_updates"] += 1
                continue

            kpi_key = detect_tag(shape, "kpi")
            if kpi_key and kpi_key in scalar_values:
                set_shape_text(shape, format_value(scalar_values[kpi_key], scalar_format.get(kpi_key, "auto")))
                stats["kpi_updates"] += 1
                continue

            text = shape_text(shape)
            if text:
                kpi_match = TAG_KPI.search(text)
                if kpi_match and kpi_match.group(1) in scalar_values:
                    key = kpi_match.group(1)
                    set_shape_text(shape, format_value(scalar_values[key], scalar_format.get(key, "auto")))
                    stats["marker_updates"] += 1
                    continue

                chart_match = TAG_CHART.search(text)
                if (chart_match and chart_match.group(1) not in MANUAL_CHART_KEYS
                        and chart_match.group(1) in chart_images):
                    key = chart_match.group(1)
                    replace_shape_with_picture(slide, shape, chart_images[key], key)
                    stats["chart_updates"] += 1
                    continue

            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        url = run.hyperlink.address or ""
                        if not url:
                            continue
                        for dashboard_id, key in link_kpi_fallbacks.items():
                            if dashboard_id in url and key in scalar_values:
                                run.text = format_value(scalar_values[key], scalar_format.get(key, "auto"))
                                stats["linked_text_updates"] += 1
                                break

            # Roll month labels forward (e.g. "Current data until 2026.06" -> "... 2026.07").
            stats["month_updates"] += apply_text_replacements(shape, month_replacements)

    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue(), stats
