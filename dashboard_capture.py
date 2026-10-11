"""Screenshot individual Databricks dashboard tiles for insertion into the deck.

Reuses the SSO session saved by dashboard_login.py to open each dashboard headlessly and
capture the specific tiles listed in kpis.DASHBOARD_TILES. Each capture becomes a PNG keyed
by its chart:<key>, which pptx_refresher then drops into the matching picture placeholder.

Design goals:
- Never hard-fail the whole deck for one bad tile. A tile that can't be found is skipped and
  reported as a warning; the app then falls back to a matplotlib render for that key.
- Load each dashboard page only once, even when several tiles come from it.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_SESSION_PATH = Path(__file__).parent / ".databricks_session.json"

# Databricks dashboards are heavy SPAs; give tiles time to render after the page settles.
PAGE_TIMEOUT_MS = 60_000
# A dashboard page may genuinely need a minute to start, but an individual tile that is absent or
# renamed should fail fast.  Using PAGE_TIMEOUT_MS for both native export and fallback lookup made
# one missing tile look like a multi-minute application hang.
TILE_TIMEOUT_MS = 15_000
DOWNLOAD_TIMEOUT_MS = 20_000
RENDER_SETTLE_MS = 6_000
# Larger viewport + 2x device scale = higher-resolution tile screenshots that stay sharp
# when scaled into the PowerPoint placeholders (the default 1x looked blurry in the deck).
VIEWPORT = {"width": 1920, "height": 1200}
# Default used for every PowerPoint chart tile. A narrow/tall viewport makes Lakeview reflow the
# visualization to the aspect ratio of a slide card instead of returning a very wide, shallow
# canvas. At 3x density this normally produces roughly 1900x1250 usable PNGs after trimming.
# Individual widgets may override this through `capture_viewport` when their card is extra-wide.
CARD_CAPTURE_VIEWPORT = {"width": 900, "height": 1000}
# 3x device scale = very high-res tile PNGs, so they stay crisp even when stretched to fill
# a placeholder box (the deck stretches wide tiles into squarer boxes).
CAPTURE_SCALE = 3

# Databricks overlays a floating "Ask Genie" button on top of widgets, which lands in the
# middle of screenshots. It has obfuscated CSS classes and React re-adds it after any hide,
# so a MutationObserver (installed at page init) continuously hides it on every DOM change.
HIDE_OVERLAYS_INIT_JS = """
    const hide = () => {
        // Hide the floating "Ask Genie" assistant pill. It survives an exact-text match
        // (icon + whitespace inside), so match any element whose text CONTAINS "Genie",
        // plus known assistant/kebab testids and fixed-position floating buttons.
        document.querySelectorAll('button, a, div[role="button"], [class*="ssistant"], [class*="enie"]').forEach(el => {
            const t = (el.textContent || '').trim();
            if (/Genie/i.test(t) || /Ask\\s*Genie/i.test(el.getAttribute('aria-label') || '')) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
            }
        });
        // Kebab triggers are hidden from screenshots UNLESS __allowKebab is set (the native
        // PNG export path needs to open the kebab menu).
        if (!window.__allowKebab) {
            document.querySelectorAll('[data-testid="kebab-context-menu-trigger"]').forEach(el => {
                el.style.setProperty('display', 'none', 'important');
            });
        }
        document.querySelectorAll('[data-testid*="genie" i], [data-testid*="assistant" i]').forEach(el => {
            el.style.setProperty('display', 'none', 'important');
        });
    };
    const start = () => {
        hide();
        try { new MutationObserver(hide).observe(document.body, {childList: true, subtree: true}); }
        catch (e) {}
    };
    if (document.body) start();
    else document.addEventListener('DOMContentLoaded', start);
"""

# WSLg's virtual GPU can make Chromium paint blank pages; force software rendering.
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--use-gl=swiftshader",
    "--in-process-gpu",
    "--disable-dev-shm-usage",
]


class SessionMissingError(RuntimeError):
    """Raised when the saved Databricks session file is absent or unreadable."""


def _group_by_dashboard(tile_map: dict) -> dict[str, list[tuple[str, dict]]]:
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for chart_key, spec in tile_map.items():
        grouped.setdefault(spec["dashboard"], []).append((chart_key, spec))
    return grouped


def _select_newest_month(page, filter_title: str = "Reporting Month") -> str | None:
    """Set a dashboard's single-select month filter to its NEWEST value, so headless capture
    shows the latest reporting period instead of the (unfiltered) default = all months.

    Databricks renders the filter as WidgetFrame.Parent-<Title> (spaces→_) holding an
    input[role=combobox]; the option list items carry data-testid="<YYYY.MM>". We open the combo
    (force-click — an overlay div intercepts a normal click), read the YYYY.MM options, and click
    the max one. Returns the month selected, or None if no such filter / no month options.

    Applies to EVERY dashboard that has a month filter (revenue + the Analytics KPI deck's
    community / learnings / trainings / events), per the ingestion+capture playbook.
    """
    testid = "WidgetFrame.Parent-" + re.sub(r"[^A-Za-z0-9]", "_", filter_title)
    frame = page.locator(f'[data-testid="{testid}"]').first
    if not frame.count():
        return None
    try:
        combo = frame.locator('input[role="combobox"]').first
        if not combo.count():
            return None
        combo.scroll_into_view_if_needed(timeout=PAGE_TIMEOUT_MS)
        combo.click(force=True)
        page.wait_for_timeout(2000)
        # Databricks uses several month representations across dashboards: `2026.07`, `2026-07`,
        # and (for DATE dimensions) `Jul 01, 2026, 12:00:00 AM`. Read the visible virtual-list
        # options rather than the hidden accessibility list, which exposes only numeric indexes.
        options = page.evaluate(
            r"""() => [...document.querySelectorAll('.du-bois-light-select-item-option')]
                .map(el => {
                    const label = (el.getAttribute('label') || el.innerText || '').trim();
                    let ts = Date.parse(label);
                    let m = label.match(/^(\d{4})[.-](\d{2})$/);
                    if (m) ts = Date.UTC(Number(m[1]), Number(m[2]) - 1, 1);
                    return {label, ts};
                })
                .filter(x => x.label && x.label.toLowerCase() !== 'all' && Number.isFinite(x.ts));"""
        )
        if not options:
            page.keyboard.press("Escape")
            return None
        newest = max(options, key=lambda x: x["ts"])
        opt = page.get_by_text(newest["label"], exact=True).last
        if not opt.count():
            page.keyboard.press("Escape")
            return None
        opt.click(force=True)
        page.wait_for_timeout(1500)
        page.keyboard.press("Escape")
        # Give the widgets time to recompute for the newly selected month.
        page.wait_for_timeout(5000)
        from datetime import datetime
        return datetime.fromtimestamp(newest["ts"] / 1000).strftime("%Y.%m")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def _refresh_dashboard_cache(page) -> bool:
    """Trigger the active dashboard sheet's on-demand query refresh."""
    try:
        button = page.locator('button[aria-label^="Refresh"]').first
        button.wait_for(state="attached", timeout=30_000)
        button.click(force=True)
        # Dashboard queries fan out asynchronously. A fixed settle is more reliable than
        # network-idle because Databricks continuously polls in the background.
        page.wait_for_timeout(15_000)
        return True
    except Exception:
        return False


def _refresh_all_dashboard_sheets(page) -> int:
    """Visit and refresh every visible dashboard sheet, returning the successful refresh count.

    Databricks refreshes only the active sheet. The old cache warmer clicked Refresh once on the
    default overview, leaving Interest Insights, Workforce Reach and every other tab on stale query
    results. Re-resolve each tab by name after every click because the SPA can remount the tab bar.
    """
    try:
        # The Databricks shell appears several seconds before the dashboard SPA. Enumerating tabs
        # too early looks exactly like a legitimate single-sheet dashboard, so wait for the shared
        # Refresh control that is mounted with the dashboard content first.
        page.locator('button[aria-label^="Refresh"]').first.wait_for(
            state="attached", timeout=30_000
        )
        page.wait_for_timeout(1_000)
        tab_names = []
        for value in page.get_by_role("tab").all_inner_texts():
            name = str(value).strip()
            if name and name not in tab_names:
                tab_names.append(name)
    except Exception:
        tab_names = []

    if not tab_names:
        return 1 if _refresh_dashboard_cache(page) else 0

    refreshed = 0
    for name in tab_names:
        try:
            page.get_by_role("tab", name=name, exact=True).first.click(timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(RENDER_SETTLE_MS)
            # Tiles render lazily; mount the complete sheet before asking Databricks to rerun it.
            _load_all_widgets(page)
            if _refresh_dashboard_cache(page):
                refreshed += 1
        except Exception:
            continue
    return refreshed


def refresh_dashboard_caches(dashboards: dict[str, str]) -> list[str]:
    """Refresh every published dashboard sheet immediately after publication."""
    if not dashboards:
        return []
    if not DEFAULT_SESSION_PATH.exists():
        return ["⚠ Dashboard cache refresh skipped: saved Databricks browser session is missing."]
    lines: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        context = browser.new_context(
            storage_state=str(DEFAULT_SESSION_PATH), viewport=VIEWPORT, ignore_https_errors=True
        )
        page = context.new_page()
        try:
            for key, url in dashboards.items():
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(RENDER_SETTLE_MS)
                refreshed = _refresh_all_dashboard_sheets(page)
                lines.append(
                    f"Refreshed published dashboard cache across {refreshed} sheet(s): {key}."
                    if refreshed
                    else f"⚠ Could not trigger published dashboard cache refresh: {key}."
                )
        finally:
            browser.close()
    return lines


def _click_tab(page, tab_name: str) -> None:
    """Switch to a named dashboard tab and let its widgets render."""
    # Lakeview mounts the shell several seconds before the sheet strip. Wait for the named tab
    # itself instead of checking ``count()`` immediately (which falsely reports a missing sheet
    # while the SPA is still starting).
    tab = page.get_by_role("tab", name=tab_name, exact=True).first
    try:
        tab.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Current Lakeview also renders sheet names as buttons; keep this fallback for DOM changes.
        tab = page.get_by_role("button", name=tab_name, exact=True).first
        tab.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    try:
        # Prefer a real pointer click, but do not spend the full page timeout retrying when
        # Lakeview's transient Refresh status portal overlaps the sheet strip.
        tab.click(timeout=3_000)
    except PlaywrightTimeoutError:
        # Keep a real pointer event (Lakeview does not reliably react to element.click()), but
        # remove hit-testing only from its unrelated portal layer. The style also covers portal
        # nodes React may remount while the click is retried.
        page.add_style_tag(
            content='[data-mfe-portal] { pointer-events: none !important; }'
        )
        tab.click(timeout=10_000)
    if tab.get_attribute("aria-selected") != "true":
        raise PlaywrightTimeoutError(f"Dashboard tab did not activate: {tab_name}")
    page.wait_for_timeout(RENDER_SETTLE_MS)
    _load_all_widgets(page)


def _widget_testid(title: str) -> str:
    """Databricks names each tile's container data-testid="WidgetFrame.Parent-<Title>",
    where every non-alphanumeric character in the title is replaced by an underscore
    (e.g. "Registration Reach % by Country" -> "Registration_Reach___by_Country")."""
    return "WidgetFrame.Parent-" + re.sub(r"[^A-Za-z0-9]", "_", title)


def _load_all_widgets(page) -> None:
    """Dashboard tiles render lazily as they scroll into view. Scroll to the bottom (and
    back up) so every widget mounts into the DOM before we try to locate it."""
    try:
        prev = -1
        for _ in range(12):
            height = page.evaluate("document.body.scrollHeight")
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(700)
            if height == prev:
                break
            prev = height
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception:
        pass  # best-effort; locating the tile will still be attempted


# Inside a tile frame, the actual plotted chart (no title bar, no side legend) is the Vega
# render container. Screenshotting this instead of the whole frame gives a clean chart image
# that fits the slide placeholders far better. Ordered by preference.
CHART_INNER_SELECTORS = (
    ".vega-worker-renderer",
    '[data-testid="individual-chart-renderer-container"]',
    '[data-testid="chart-renderer-container"]',
)


def _hide_embedded_chart_title(frame) -> None:
    """Hide Vega's title layer; the PowerPoint card already supplies the visible heading."""
    try:
        frame.evaluate(
            """root => {
                root.querySelectorAll(
                    '.role-title, g[aria-label^="Title"], [data-testid="visualization-title"]'
                ).forEach(el => { el.style.display = 'none'; });
            }"""
        )
    except Exception:
        pass


def _trim_whitespace(
    path: Path, padding: int = 12, min_fill: float = 0.02, drop_title: bool = True
) -> None:
    """Crop background borders (and the chart's own title band) from a captured PNG in place.

    Vega renders the chart in a wide container with lots of empty margin (and a faint
    full-width axis baseline that a naive bbox trim would keep). This trims any row/column
    that is almost entirely background — kept only if more than `min_fill` of its pixels
    differ from the background — so thin stray lines don't block the crop.

    When `drop_title` is set, the chart's own small title line (e.g. "Completions",
    "Respondents") is removed: Vega paints it as a thin content band at the very top,
    separated from the chart body by a blank gap. We detect that first gap and cut above it.
    """
    try:
        import numpy as np

        img = Image.open(path).convert("RGB")
        arr = np.asarray(img)
        bg = arr[0, 0]  # top-left pixel = background colour
        # Per-pixel: does it differ meaningfully from the background?
        diff = np.abs(arr.astype(int) - bg.astype(int)).sum(axis=2) > 24
        col_fill = diff.mean(axis=0)  # fraction of non-bg pixels per column
        row_fill = diff.mean(axis=1)  # ... per row
        cols = np.where(col_fill > min_fill)[0]
        rows = np.where(row_fill > min_fill)[0]
        if cols.size == 0 or rows.size == 0:
            return

        top_idx = int(rows[0])
        title_dropped = False
        if drop_title:
            # Walk down from the first content row. If a short content band (the title) is
            # followed by a blank gap, treat everything up to the gap as title and drop it.
            content_rows = set(rows.tolist())
            blank_run = 0
            title_end = None
            for y in range(top_idx, int(rows[-1])):
                if y in content_rows:
                    if blank_run >= 4 and (y - top_idx) < img.height * 0.20:
                        title_end = y  # chart body resumes here, after a gap
                        break
                    blank_run = 0
                else:
                    blank_run += 1
            if title_end is not None:
                top_idx = title_end
                title_dropped = True

        left = max(0, int(cols[0]) - padding)
        right = min(img.width, int(cols[-1]) + 1 + padding)
        # When we've cut the title, don't re-pad the top back into it.
        top = top_idx if title_dropped else max(0, top_idx - padding)
        bottom = min(img.height, int(rows[-1]) + 1 + padding)

        # Safety: if the proposed crop would keep less than 30% of either dimension (i.e.
        # remove >70%), something went wrong (e.g. a near-blank map read as mostly background,
        # or a stray content row confusing the title detection). Keep the untrimmed image.
        keep_w = (right - left) / img.width if img.width else 1.0
        keep_h = (bottom - top) / img.height if img.height else 1.0
        if keep_w < 0.30 or keep_h < 0.30:
            return

        img.crop((left, top, right, bottom)).save(path)
    except Exception:
        pass  # best-effort; keep the untrimmed image if anything goes wrong


def _validate_capture_has_visual(path: Path, min_content: float = 0.04) -> None:
    """Reject blank/error cards that Lakeview sometimes returns while queries recompute."""
    import numpy as np

    img = Image.open(path).convert("RGB")
    # Work on a small copy so validation remains cheap for 3x retina screenshots.
    img.thumbnail((600, 600))
    arr = np.asarray(img)
    if not arr.size:
        raise PlaywrightTimeoutError(f"Captured image is empty: {path.name}")
    bg = arr[0, 0]
    content = np.abs(arr.astype(int) - bg.astype(int)).sum(axis=2) > 24
    if float(content.mean()) < min_content:
        raise PlaywrightTimeoutError(
            f"Captured tile is blank or shows a Databricks render error: {path.name}"
        )


def _crop_lakeview_header(path: Path, header_css_px: int = 18) -> None:
    """Remove the duplicated Lakeview widget title while retaining the complete chart body."""
    try:
        img = Image.open(path)
        cut = min(img.height // 4, header_css_px * CAPTURE_SCALE)
        if cut > 0 and img.height - cut >= 80:
            img.crop((0, cut, img.width, img.height)).save(path)
    except Exception:
        pass


def _apply_fractional_crop(path: Path, crop: tuple[float, float, float, float] | list) -> None:
    """Apply a tile-specific proportional crop `(left, top, right, bottom)`."""
    try:
        img = Image.open(path)
        left, top, right, bottom = crop
        box = (
            max(0, int(img.width * left)),
            max(0, int(img.height * top)),
            min(img.width, int(img.width * right)),
            min(img.height, int(img.height * bottom)),
        )
        if box[2] > box[0] and box[3] > box[1]:
            img.crop(box).save(path)
    except Exception:
        pass


def _trim_edges_strong(
    path: Path, padding: int = 14, thresh: int = 60, drop_title: bool = True
) -> None:
    """Trim outer margins using a STRONG difference threshold, optionally dropping the title band.

    Companion to `_trim_whitespace` for the `_capture_svg_overflow` branch. That clip is already
    tight to the measured content union, but the union can still include a faint sliver of the
    neighbouring widget's scrollbar (pixel diff ~46 vs the ~250 of real ink). The default trim's
    thresh=24 counts that sliver as content and keeps it; raising the bar to 60 drops it while
    every real mark (title, axis ticks, legend glyphs) clears it easily.

    With `drop_title` (the default) the chart's own baked-in header (e.g. "Revenue by Country") is
    removed: it duplicates the slide card's title label. Vega paints it as a short content band at
    the very top, separated from the plot by a blank gap — we cut at that gap. Detection runs at
    the STRONG threshold so the faint neighbour sliver can't masquerade as the title band.
    """
    try:
        import numpy as np

        img = Image.open(path).convert("RGB")
        arr = np.asarray(img).astype(int)
        bg = arr[0, 0]
        strong = np.abs(arr - bg).sum(axis=2) > thresh
        cols = np.where(strong.mean(axis=0) > 0.0005)[0]
        rows = np.where(strong.mean(axis=1) > 0.0005)[0]
        if cols.size == 0 or rows.size == 0:
            return

        top_idx = int(rows[0])
        title_dropped = False
        if drop_title:
            # Walk down from the first content row: a short band followed by a blank gap is the
            # title. Require the gap to be a real separation (>=6 rows at this resolution) and the
            # band to sit in the top 20% so a tall plot is never mistaken for a header.
            content_rows = set(rows.tolist())
            blank_run = 0
            for y in range(top_idx, int(rows[-1])):
                if y in content_rows:
                    if blank_run >= 6 and (y - top_idx) < img.height * 0.20:
                        top_idx = y  # plot body resumes here, after the gap
                        title_dropped = True
                        break
                    blank_run = 0
                else:
                    blank_run += 1

        left = max(0, int(cols[0]) - padding)
        right = min(img.width, int(cols[-1]) + 1 + padding)
        # Once the title is cut, don't re-pad upward — that would bring it back.
        top = top_idx if title_dropped else max(0, top_idx - padding)
        bottom = min(img.height, int(rows[-1]) + 1 + padding)

        # Same >70%-removed guard as _trim_whitespace: a sparse tile read as mostly background
        # must not be mangled.
        if (right - left) / img.width < 0.30 or (bottom - top) / img.height < 0.30:
            return
        if (left, top, right, bottom) == (0, 0, img.width, img.height):
            return
        img.crop((left, top, right, bottom)).save(path)
    except Exception:
        pass  # best-effort; keep the image as captured


def _download_widget_png(page, widget_id: str, spec: dict, output_dir: Path, chart_key: str) -> Path | None:
    """Use Databricks' OWN 'Download PNG' export for a widget (kebab menu → Download →
    Download PNG). This yields the exact Databricks-rendered image (correct colors, legend,
    labels, no browser-scaling artifacts) — the preferred, highest-quality source.

    Returns the saved PNG path, or None if this widget offers no PNG export (caller then
    falls back to a high-res screenshot). The kebab is normally hidden by our overlay CSS;
    we force it visible for the duration of the click.
    """
    try:
        frame = page.locator(f'[data-testid$="~{widget_id}"]').first
        frame.wait_for(state="visible", timeout=TILE_TIMEOUT_MS)
        frame.scroll_into_view_if_needed(timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(spec.get("settle_ms", 1_500))
        # Our HIDE_OVERLAYS observer keeps re-hiding kebab triggers. Flip __allowKebab so it
        # stops hiding them, and clear any inline display:none already applied, so a real
        # Playwright pointer click can open the Radix menu (a synthetic DOM .click() does not).
        page.evaluate(
            "() => { window.__allowKebab = true;"
            " document.querySelectorAll('[data-testid=\\'kebab-context-menu-trigger\\']')"
            "  .forEach(el => el.style.removeProperty('display')); }"
        )
        kebab = frame.locator('[data-testid="kebab-context-menu-trigger"]').first
        if not kebab.count():
            return None
        kebab.click(timeout=15_000)
        page.wait_for_timeout(900)
        download_item = page.get_by_role("menuitem", name="Download")
        if not download_item.count():
            page.keyboard.press("Escape")
            return None
        download_item.first.hover()
        page.wait_for_timeout(800)
        png_item = page.get_by_role("menuitem", name="Download PNG")
        if not png_item.count():
            page.keyboard.press("Escape")
            return None
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            png_item.first.click()
        download = dl_info.value
        path = output_dir / f"{chart_key}.png"
        download.save_as(str(path))
        return path if path.exists() and path.stat().st_size > 0 else None
    except Exception:
        return None  # any failure → caller falls back to screenshot
    finally:
        # Re-enable kebab hiding so it stays out of subsequent tile screenshots.
        try:
            page.evaluate("() => { window.__allowKebab = false; }")
        except Exception:
            pass


def _capture_svg_overflow(page, frame, path: Path) -> bool:
    """Capture a Vega chart whose LEGEND TEXT overflows the SVG's own layout box.

    element.screenshot() clips to the SVG's box, but Vega paints legend labels
    ("Country"/"Germany") past the right edge with overflow:visible → they get cut. Instead we
    measure the UNION of every descendant rect inside the SVG (text nodes included, so the
    overflowing legend is counted) and clip the page to exactly that rectangle. The union ends
    right after the legend text, so it never reaches the neighbouring tile's scrollbar.

    Returns True if it captured, False to let the caller fall back to element.screenshot().
    """
    try:
        handle = frame.element_handle()
        if handle is None:
            return False
        box = page.evaluate(
            """(root) => {
                // The plot is a <canvas>; the colour legend is SEPARATE sibling elements (small
                // swatch svgs + label text) positioned to its right, overflowing the frame's
                // clipping edge. So measure the union of every element inside the frame — that
                // covers canvas AND legend — instead of hunting for one SVG that holds both.
                const fr = root.getBoundingClientRect();
                let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity, n = 0;
                root.querySelectorAll('*').forEach((el) => {
                    const c = el.getBoundingClientRect();
                    if (!c.width || !c.height) return;
                    // Skip scroll/overlay containers much larger than the frame, and anything
                    // far outside it (tooltips parked offscreen) — they'd blow the union open.
                    if (c.width > fr.width * 1.5 && c.height > fr.height * 1.5) return;
                    if (c.right < fr.left - 40 || c.left > fr.right + 400) return;
                    if (c.bottom < fr.top - 40 || c.top > fr.bottom + 40) return;
                    l = Math.min(l, c.left); t = Math.min(t, c.top);
                    r = Math.max(r, c.right); b = Math.max(b, c.bottom); n++;
                });
                if (!isFinite(l) || !n) return null;
                return {x: l, y: t, w: r - l, h: b - t,
                        fl: fr.left, ft: fr.top, fr: fr.right, fb: fr.bottom};
            }""",
            handle,
        )
        if not box or box["w"] < 50 or box["h"] < 50:
            return False
        # Breathing room. SVG <text> getBoundingClientRect under-reports the glyph extent, so the
        # "Country" legend header and "20B" axis tick sit ABOVE the measured union top and get
        # clipped without extra room. The top needs the most; the right needs enough for the last
        # legend glyph but must stay short of the neighbouring tile's scrollbar.
        pad = 8
        top_pad = 34
        vw = page.viewport_size["width"]
        vh = page.viewport_size["height"]
        # The legend overflows the frame to the RIGHT only. Padding the other three sides past the
        # frame's own edge reaches into the neighbouring widgets — that is how the tile above leaked
        # its pagination row ("< 1 2 3 ... >") into the top of the revenue capture. So clamp top,
        # left and bottom to the frame rect and let only the right edge overflow.
        x = max(0, box["x"] - pad, box["fl"])
        y = max(0, box["y"] - top_pad, box["ft"])
        w = min(vw - x, box["w"] + 2 * pad + (box["x"] - x))
        h = min(vh - y, box["h"] + (box["y"] - y) + pad, max(0.0, box["fb"] - y))
        if w < 50 or h < 50:
            return False
        page.screenshot(path=str(path), clip={"x": x, "y": y, "width": w, "height": h})
        return True
    except Exception:
        return False


def _pick_target(frame, spec: dict, full_tile: bool):
    """Choose what element to screenshot inside a tile frame.

    - spec["capture_svg"]=True → the Vega SVG element (.vega-worker-renderer svg). Use this when
      the legend text OVERFLOWS the widget frame's right edge (a frame screenshot would clip it);
      the SVG contains the whole chart + full legend. Falls back to the frame if not found.
    - full_tile → the whole frame (title + legend), the default.
    - otherwise → the inner chart element (no title/legend).
    """
    if spec.get("capture_svg"):
        for sel in ("svg.marks", ".vega-worker-renderer svg", ".vega-worker-renderer"):
            el = frame.locator(sel).first
            if el.count():
                return el
    if not full_tile:
        for sel in CHART_INNER_SELECTORS:
            inner = frame.locator(sel).first
            if inner.count():
                return inner
    return frame


def _screenshot_tile(page, chart_key: str, spec: dict, output_dir: Path) -> Path:
    """Capture one dashboard tile as a PNG.

    Strategy, in order:
      1. Locate the tile frame by its Databricks widget id
         (data-testid="WidgetFrame.Parent-<title>").
      2. Screenshot the inner CHART element (no title bar / legend) when present, so the
         image fits the slide box; otherwise fall back to the whole frame.
      3. Fall back to a manual crop rectangle if provided.

    Set spec["full_tile"]=True to force capturing the entire tile (title + legend) instead
    of just the chart — useful for tiles where the legend is essential.
    """
    path = output_dir / f"{chart_key}.png"
    crop = spec.get("crop")
    title = spec.get("title")
    widget_id = spec.get("widget_id")
    # Capture the bounded widget wrapper, then remove its title band. Trying to screenshot nested
    # renderer/card elements is unsafe in Lakeview: some inner cards span the next grid row too.
    full_tile = spec.get("full_tile", True)
    # PowerPoint supplies every card title, so Databricks' duplicate widget title is always
    # removed. This is intentionally global rather than a per-tile option. Axes, legends, labels,
    # and the first data row remain part of the captured chart body.
    drop_title = True
    trim = spec.get("trim", True)

    # Some tiles (e.g. counters, or charts with the title hidden) have no matchable title.
    # Capture those by their Databricks widget id via the inner card container testid
    # (data-testid="widget-card-inner-<page>~<widget_id>"), matched by suffix.
    if widget_id:
        try:
            frame = page.locator(f'[data-testid$="~{widget_id}"]').first
            frame.wait_for(state="visible", timeout=TILE_TIMEOUT_MS)
            frame.scroll_into_view_if_needed(timeout=TILE_TIMEOUT_MS)
            page.mouse.move(5, 5)
            # Mapbox/WebGL tiles need extra time to finish painting after scroll-in.
            page.wait_for_timeout(spec.get("settle_ms", 1_500))
            _hide_embedded_chart_title(frame)
            captured = False
            if spec.get("capture_svg"):
                captured = _capture_svg_overflow(page, frame, path)
            if not captured:
                target = _pick_target(frame, spec, full_tile)
                target.screenshot(path=str(path))
                if full_tile and drop_title:
                    _crop_lakeview_header(path)
                if spec.get("fractional_crop"):
                    _apply_fractional_crop(path, spec["fractional_crop"])
            if trim and not captured:
                _trim_whitespace(path, drop_title=drop_title)
            elif trim:
                # The overflow clip is already tight to the measured content union, so the normal
                # trim would re-eat the thin "Country"/"20B" top band (min_fill reads it as bg).
                # It can still carry the neighbour's faint scrollbar sliver on the right edge —
                # strip that with the strong-threshold edge trim, which also drops the baked-in
                # header (it duplicates the slide card's own title label).
                _trim_edges_strong(path, drop_title=drop_title)
            _validate_capture_has_visual(path)
            return path
        except PlaywrightTimeoutError:
            # Lakeview may remount widgets with a changed/temporarily absent internal id.
            # Continue to the stable title lookup before resorting to a manual crop.
            pass

    if title:
        try:
            frame = page.locator(f'[data-testid="{_widget_testid(title)}"]').first
            frame.wait_for(state="visible", timeout=TILE_TIMEOUT_MS)
            frame.scroll_into_view_if_needed(timeout=TILE_TIMEOUT_MS)
            # Move the mouse away so the "Ask Genie" hover button isn't triggered; the
            # init-script MutationObserver keeps it hidden even if React re-adds it.
            page.mouse.move(5, 5)
            page.wait_for_timeout(1_500)  # let the tile finish painting after scroll
            _hide_embedded_chart_title(frame)

            captured = False
            if spec.get("capture_svg"):
                captured = _capture_svg_overflow(page, frame, path)
            if not captured:
                target = _pick_target(frame, spec, full_tile)
                target.screenshot(path=str(path))
                if full_tile and drop_title:
                    _crop_lakeview_header(path)
                if spec.get("fractional_crop"):
                    _apply_fractional_crop(path, spec["fractional_crop"])
            if trim and not captured:
                _trim_whitespace(path, drop_title=drop_title)
            elif trim:
                # see the widget_id branch for why
                _trim_edges_strong(path, drop_title=drop_title)
            _validate_capture_has_visual(path)
            return path
        except PlaywrightTimeoutError:
            if not crop:
                raise
            # fall through to crop fallback

    if crop:
        page.screenshot(
            path=str(path),
            clip={
                "x": crop["x"],
                "y": crop["y"],
                "width": crop["width"],
                "height": crop["height"],
            },
        )
        _validate_capture_has_visual(path)
        return path

    raise PlaywrightTimeoutError(f"Tile not found and no crop fallback: {chart_key}")


def capture_dashboard_images(
    tile_map: dict,
    dashboards: dict,
    output_dir: Path,
    session_path: Path = DEFAULT_SESSION_PATH,
    filter_newest_month: bool = True,
    on_progress=None,
) -> tuple[dict[str, Path], list[str]]:
    """Capture every tile in tile_map.

    Returns (images, warnings):
      images   -> {chart_key: png_path} for tiles captured successfully
      warnings -> human-readable strings for tiles that were skipped
    Raises SessionMissingError if the saved session file does not exist.
    """
    session_path = Path(session_path)
    if not session_path.exists():
        raise SessionMissingError(
            f"No saved Databricks session at {session_path}. "
            "Run: .venv/bin/python dashboard_login.py"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    images: dict[str, Path] = {}
    warnings: list[str] = []
    grouped = _group_by_dashboard(tile_map)
    completed = 0
    total = len(tile_map)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        context = browser.new_context(
            storage_state=str(session_path),
            viewport=VIEWPORT,
            # Capture at 2x pixel density (retina) so tile text/lines stay crisp when the
            # PNG is scaled up inside the PowerPoint placeholder. Without this, images look
            # soft/blurry in the deck.
            device_scale_factor=CAPTURE_SCALE,
            # Allow Databricks' native "Download PNG" export to save a file.
            accept_downloads=True,
            # Corporate firewall does TLS interception with an untrusted root CA; accept it
            # so dashboard pages load instead of failing with a cert error.
            ignore_https_errors=True,
        )
        context.add_init_script(HIDE_OVERLAYS_INIT_JS)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            for dashboard_key, tiles in grouped.items():
                url = dashboards.get(dashboard_key)
                if not url:
                    warnings.append(
                        f"Dashboard '{dashboard_key}' has no URL in DATABRICKS_DASHBOARDS; "
                        f"skipped {len(tiles)} tile(s)."
                    )
                    continue
                # Databricks dashboards poll continuously, so "networkidle" never fires —
                # load on DOM ready and rely on the render-settle delay + scroll instead.
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(RENDER_SETTLE_MS)
                if _refresh_dashboard_cache(page):
                    warnings.append(f"[info] '{dashboard_key}': refreshed published query cache.")
                _load_all_widgets(page)

                # If the saved session expired, Databricks redirects to a login page.
                if "login" in page.url.lower() or "aad" in page.url.lower():
                    raise SessionMissingError(
                        "Databricks session appears expired (redirected to login). "
                        "Re-run: .venv/bin/python dashboard_login.py"
                    )

                # Set the month filter to the NEWEST period so captured tiles reflect the latest
                # data just loaded (dashboards default to all-months / unfiltered otherwise).
                if filter_newest_month:
                    picked = _select_newest_month(page)
                    if picked:
                        warnings.append(f"[info] '{dashboard_key}': filtered to newest month {picked}.")
                        # Do NOT re-scroll/reload here — that races and resets the filter. Widgets
                        # are already mounted; just let them recompute for the selected month.
                        page.wait_for_timeout(4000)

                # Capture tiles grouped by tab, so we click each tab only once.
                current_tab = None
                for chart_key, spec in sorted(tiles, key=lambda kv: kv[1].get("tab") or ""):
                    tab = spec.get("tab")
                    try:
                        # Per-tile viewport: narrow viewports make wide tiles (esp. Mapbox maps)
                        # REFLOW to a squarer aspect (playbook step 3/4). Resize + let it re-render.
                        cv = spec.get("capture_viewport", CARD_CAPTURE_VIEWPORT)
                        page.set_viewport_size(cv)
                        # Resizing changes Vega's layout, label density and canvas dimensions.
                        # Always wait for that reflow—even for charts without an explicit override.
                        page.wait_for_timeout(spec.get("settle_ms", 6000))
                        if tab and tab != current_tab:
                            _click_tab(page, tab)
                            current_tab = tab
                            # Reporting-month filters are sheet-local in Lakeview. Selecting July
                            # on the Overview does not select July on Workforce Reach or Interest
                            # Insights, so apply the newest value again after every tab switch.
                            if filter_newest_month:
                                picked = _select_newest_month(page)
                                if picked:
                                    warnings.append(
                                        f"[info] '{dashboard_key}/{tab}': filtered to newest "
                                        f"month {picked}."
                                    )
                        # PREFERRED: use Databricks' own 'Download PNG' export (exact rendered
                        # image, no browser-scaling). Only when the widget offers it and the
                        # tile isn't opted out via spec["native_export"]=False.
                        saved = None
                        widget_id = spec.get("widget_id")
                        # Direct element screenshots preserve the exact on-screen Databricks
                        # rendering and are much faster. Native PNG export is opt-in because most
                        # widgets expose a menu item that never emits a download in headless mode,
                        # costing DOWNLOAD_TIMEOUT_MS before every otherwise successful capture.
                        if widget_id and spec.get("native_export", False):
                            saved = _download_widget_png(
                                page, widget_id, spec, output_dir, chart_key
                            )
                            if saved is not None:
                                warnings.append(
                                    f"[info] '{chart_key}': used Databricks native PNG export."
                                )
                        # FALLBACK: high-resolution element screenshot.
                        if saved is None:
                            try:
                                saved = _screenshot_tile(page, chart_key, spec, output_dir)
                            except PlaywrightTimeoutError:
                                # Lakeview mounts sheet widgets lazily and can briefly remove them
                                # while refreshed queries settle. Remount the sheet and retry once
                                # before declaring an exact dashboard visual missing.
                                _load_all_widgets(page)
                                page.wait_for_timeout(3_000)
                                saved = _screenshot_tile(page, chart_key, spec, output_dir)
                        images[chart_key] = saved
                    except Exception as exc:  # noqa: BLE001 - one bad tile must not abort all
                        warnings.append(
                            f"Could not capture '{chart_key}' "
                            f"(tab '{spec.get('tab', '-')}', tile '{spec.get('title', '?')}'): {exc}"
                        )
                    finally:
                        completed += 1
                        if on_progress is not None:
                            on_progress(completed, total, chart_key)
        finally:
            browser.close()

    return images, warnings


def list_dashboard_tiles(
    dashboards: dict,
    session_path: Path = DEFAULT_SESSION_PATH,
) -> None:
    """Print every capturable chart tile (dashboard -> tab -> title) for calibration.

    Use the printed titles to fill in kpis.DASHBOARD_TILES. Run with:
        .venv/bin/python -m dashboard_capture --list
    """
    session_path = Path(session_path)
    if not session_path.exists():
        raise SessionMissingError(
            f"No saved Databricks session at {session_path}. "
            "Run: .venv/bin/python dashboard_login.py"
        )

    js_chart_tiles = """() => {
        const out = [];
        document.querySelectorAll('[data-testid^="WidgetFrame.Parent-"]').forEach(f => {
            const hasChart = f.querySelector('.vega-worker-renderer, svg, canvas');
            const h = f.querySelector('h1,h2,h3,h4,[class*="itle"]');
            const title = (h ? h.innerText : '').trim();
            if (hasChart && title && !/Filter/.test(title)) out.push(title);
        });
        return [...new Set(out)];
    }"""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        context = browser.new_context(
            storage_state=str(session_path), viewport=VIEWPORT, ignore_https_errors=True
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            for dkey, url in dashboards.items():
                print(f"\n######## {dkey} ########")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(RENDER_SETTLE_MS)
                tabs = [t.inner_text().strip() for t in page.get_by_role("tab").all()]
                for tab_name in tabs or [None]:
                    if tab_name:
                        try:
                            _click_tab(page, tab_name)
                        except Exception:
                            pass
                    else:
                        _load_all_widgets(page)
                    titles = page.evaluate(js_chart_tiles)
                    print(f"  TAB '{tab_name}':")
                    for t in titles:
                        print(f"      title: {t!r}")
        finally:
            browser.close()


if __name__ == "__main__":
    import sys

    from kpis import DATABRICKS_DASHBOARDS

    if "--list" in sys.argv:
        list_dashboard_tiles(DATABRICKS_DASHBOARDS)
    else:
        print("Usage: python -m dashboard_capture --list")
